"""ui/v1, phase 3: dataset usage, the client filter, expiry and delete bodies, the reaper, storage (tj-grna9p.84, .90).

The design is tj-grna9p.84's 2026-10-13 OPTION R FINAL CHANGE LIST (ruling tj-grna9p.131) and its 2026-10-14
REWORK LIST R1-R7, which win over the bead body; the pins below are the list in the architect's .84 gate note
on tj-grna9p.90 (built in 861134c and 47e6146, comments in 582c821):

* the enums ClientKind and UsageFilter, values and numbers;
* DatasetSummary.usage = 21 and reaper_pending = 22; DatasetFacets 7..15, names, numbers and types;
* the new messages, as the full field list: DatasetUsage, ClientCount, OperatorOverride, SetExpiryRequest (expiry
  optional), DeleteDatasetRequest, ReaperPendingPage; in stats.proto UsageSummary, LastReadBucket(s),
  ReadsPerDay(+Entry), ClientLastRead, StaleDatasetRow/Page, StorageBySource, StorageCurrent;
* QuotaBurn (tj-utqd77 Q2, moved to tj-lenn8u), HealthPerDay, StorageGrowth and UiSettings are ABSENT;
* UiConfig is unchanged (OPTION R item 5: the web never needs its client id from the server);
* the phase 1 and phase 2 numbering of the two messages phase 3 appends to still holds (the phase 2 file pins
  the rest).

Everything is read from protoc's descriptors (common/tests/proto_descriptors.py), or, for the canonical JSON,
from gen/ alone in a subprocess, for the reason test_proto_ui_phase_two.py gives (TID251).
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

from common.tests.proto_descriptors import file_named, messages
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
FIELD = descriptor_pb2.FieldDescriptorProto

T, S, I64, I32, B = '.google.protobuf.Timestamp', 'TYPE_STRING', 'TYPE_INT64', 'TYPE_INT32', 'TYPE_BOOL'
M, UI = '.trader_joe.proto.market.v1.', '.trader_joe.proto.ui.v1.'
KIND = f'{UI}ClientKind'


def _ran(result: subprocess.CompletedProcess) -> str:
    return f'exit {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}'


def _shape(field: descriptor_pb2.FieldDescriptorProto) -> tuple:
    """(name, number, type, repeated, `optional`): the last is proto3 presence, which several fields here carry."""
    kind = field.type_name if field.type in (FIELD.TYPE_MESSAGE, FIELD.TYPE_ENUM) else FIELD.Type.Name(field.type)
    return (field.name, field.number, kind, field.label == FIELD.LABEL_REPEATED, field.proto3_optional)


def _f(name: str, number: int, kind: str, repeated: bool = False, optional: bool = False) -> tuple:
    return (name, number, kind, repeated, optional)


def _r(name: str, number: int, kind: str) -> tuple:
    return _f(name, number, kind, repeated=True)


# ---------------------------------------------------------------------------------------------------
# THE ENUMS


def _enum(file: str, enum: str) -> list[tuple[str, int]]:
    return [(value.name, value.number) for value in {e.name: e for e in file_named(file).enum_type}[enum].value]


def test_client_kind_names_exactly_ui_and_sdk_after_unspecified():
    """OPTION R (1). UNSPECIFIED is never stored or returned as a figure's kind; the default filter is SDK."""
    assert _enum(DATA, 'ClientKind') == [('CLIENT_KIND_UNSPECIFIED', 0), ('CLIENT_KIND_UI', 1), ('CLIENT_KIND_SDK', 2)]


def test_usage_filter_names_exactly_the_three_usage_options():
    """R2: the list's usage query filter, named per the enum-filter rule."""
    assert _enum(DATA, 'UsageFilter') == [
        ('USAGE_FILTER_UNSPECIFIED', 0),
        ('USAGE_FILTER_READ_30D', 1),
        ('USAGE_FILTER_STALE', 2),
        ('USAGE_FILTER_NEVER_READ', 3),
    ]


# ---------------------------------------------------------------------------------------------------
# APPENDED TO PHASE 1 MESSAGES: DatasetSummary AND DatasetFacets

# Each message's full numbering up to the end of phase 3, in declaration order: phase 1 as merged
# (20756e3), the phase 2 appendix (tj-grna9p.70), then phase 3. A later phase may only append after it.
THROUGH_PHASE_THREE = {
    'DatasetSummary': [
        _f('id', 1, S),
        _f('asset_symbol', 2, S),
        _f('asset_type', 3, f'{M}AssetType'),
        _f('data_type', 4, f'{M}DataType'),
        _f('source', 5, f'{M}DataSource'),
        _f('feed', 6, f'{M}Feed'),
        _f('granularity', 7, f'{M}Granularity'),
        _f('start', 8, T),
        _f('end', 9, T),
        _f('update_type', 10, f'{M}UpdateType'),
        _f('expiry_type', 11, f'{UI}ExpiryType'),
        _f('expiry', 12, T),
        _f('owner', 13, S),
        _f('state', 14, f'{UI}DatasetState'),
        _f('first_bar', 15, T),
        _f('last_bar', 16, T),
        _f('bar_count', 17, I64),
        _f('freshness', 18, f'{UI}Freshness'),
        _r('siblings', 19, f'{UI}DatasetSibling'),
        # phase 2
        _f('last_run', 20, f'{UI}IngestRun'),
        # phase 3: OPTION R (2) and R4
        _f('usage', 21, f'{UI}DatasetUsage'),
        _f('reaper_pending', 22, B),
    ],
    'DatasetFacets': [
        _f('all', 1, I64),
        _f('needs_attention', 2, I64),
        _r('sources', 3, f'{UI}SourceFacet'),
        _r('update_types', 4, f'{UI}UpdateTypeFacet'),
        _r('statuses', 5, f'{UI}StatusFacet'),
        # phase 2
        _f('running', 6, I64),
        # phase 3: OPTION R (3), then R3
        _r('clients', 7, f'{UI}ClientCount'),
        _r('client_ids', 8, S),
        _r('client_kinds', 9, KIND),
        _f('stale', 10, I64),
        _f('read_30d', 11, I64),
        _f('never_read', 12, I64),
        _f('expiring_soon', 13, I64),
        _f('stale_days', 14, I32),
        _f('expires_before', 15, T),
    ],
}


@pytest.mark.parametrize('message', sorted(THROUGH_PHASE_THREE))
def test_phase_three_appends_exactly_its_fields_after_phases_one_and_two(message: str):
    """Phases 1 and 2 are an unchanged prefix, followed by exactly the phase 3 fields; anything after is later.

    Args:
        message: A data.proto message phase 3 appends to.
    """
    fields = [_shape(field) for field in messages(file_named(DATA))[message].field]
    expected = THROUGH_PHASE_THREE[message]
    assert fields[: len(expected)] == expected


# ---------------------------------------------------------------------------------------------------
# THE NEW MESSAGES, AS THE FULL FIELD LIST

NEW_MESSAGES = {
    (DATA, 'DatasetUsage'): [
        _f('last_read_at', 1, T),
        _f('reads_30d', 2, I64),
        _r('read_by_30d', 3, KIND),
        _r('read_by_clients_30d', 4, S),
        _r('client_ids', 5, S),
        _r('client_kinds', 6, KIND),
    ],
    (DATA, 'ClientCount'): [_f('client_id', 1, S), _f('client_kind', 2, KIND), _f('datasets', 3, I64)],
    (DATA, 'ReaperPendingPage'): [
        _r('items', 1, f'{UI}DatasetSummary'),
        _f('next_cursor', 2, S),
        _f('deletes_enabled', 3, B),
        _f('as_of', 4, T),
    ],
    (DATA, 'OperatorOverride'): [_f('reason', 1, S), _f('confirmed', 2, B)],
    # Unset expiry CLEARS it, so presence is the point: `optional`.
    (DATA, 'SetExpiryRequest'): [_f('expiry', 1, T, optional=True), _f('override', 2, f'{UI}OperatorOverride')],
    (DATA, 'DeleteDatasetRequest'): [_f('override', 1, f'{UI}OperatorOverride')],
    (STATS, 'UsageSummary'): [
        _f('stale', 1, I64),
        _f('read_last_30d', 2, I64),
        _f('total', 3, I64),
        _f('never_read', 4, I64),
        _f('reads_today', 5, I64),
        _f('distinct_clients_today', 6, I64),
        _r('client_ids', 7, S),
        _r('client_kinds', 8, KIND),
        _f('stale_days', 9, I32),
    ],
    # The open last bucket has no max_days, so "unset" must not read as 0.
    (STATS, 'LastReadBucket'): [_f('min_days', 1, I32), _f('max_days', 2, I32, optional=True), _f('datasets', 3, I64)],
    (STATS, 'LastReadBuckets'): [
        _r('buckets', 1, f'{UI}LastReadBucket'),
        _f('never_read', 2, I64),
        _r('client_ids', 3, S),
        _r('client_kinds', 4, KIND),
    ],
    (STATS, 'ReadsPerDayEntry'): [
        _f('day', 1, S),
        _f('client_id', 2, S),
        _f('client_kind', 3, KIND),
        _f('reads', 4, I64),
    ],
    # R6: the echo is 2 and 3.
    (STATS, 'ReadsPerDay'): [
        _r('days', 1, f'{UI}ReadsPerDayEntry'),
        _r('client_ids', 2, S),
        _r('client_kinds', 3, KIND),
    ],
    (STATS, 'ClientLastRead'): [
        _f('client_id', 1, S),
        _f('client_kind', 2, KIND),
        _f('last_read_at', 3, T, optional=True),
    ],
    (STATS, 'StaleDatasetRow'): [
        _f('dataset', 1, f'{UI}DatasetSummary'),
        _f('est_bytes', 2, I64),
        _r('last_read_by_client', 3, f'{UI}ClientLastRead'),
    ],
    (STATS, 'StaleDatasetPage'): [
        _r('items', 1, f'{UI}StaleDatasetRow'),
        _f('next_cursor', 2, S),
        _r('client_ids', 3, S),
        _r('client_kinds', 4, KIND),
        _f('stale_days', 5, I32),
    ],
    (STATS, 'StorageBySource'): [_f('source', 1, f'{M}DataSource'), _f('feed', 2, f'{M}Feed'), _f('est_bytes', 3, I64)],
    (STATS, 'StorageCurrent'): [
        _f('total_bytes', 1, I64),
        _f('as_of', 2, T),
        _r('by_source', 3, f'{UI}StorageBySource'),
    ],
}


@pytest.mark.parametrize(('file', 'message'), sorted(NEW_MESSAGES), ids=[m for _, m in sorted(NEW_MESSAGES)])
def test_each_phase_three_message_carries_exactly_the_ruled_fields(file: str, message: str):
    """Name, number, type, cardinality and presence, as the full list: these are new, so an extra is a design change.

    Args:
        file: The .proto declaring it.
        message: The message.
    """
    assert [_shape(field) for field in messages(file_named(file))[message].field] == NEW_MESSAGES[(file, message)]


# ---------------------------------------------------------------------------------------------------
# WHAT MUST NOT EXIST, AND WHAT MUST NOT CHANGE


def _all_names() -> set[str]:
    """Every message and enum declared in ui/v1, nested ones included."""
    names = set()
    for file in UI_FILES:
        compiled = file_named(file)
        names.update(enum.name for enum in compiled.enum_type)
        stack = list(compiled.message_type)
        while stack:
            message = stack.pop()
            names.add(message.name)
            names.update(enum.name for enum in message.enum_type)
            stack.extend(message.nested_type)
    return names


@pytest.mark.parametrize('name', ['QuotaBurn', 'HealthPerDay', 'StorageGrowth', 'UiSettings'])
def test_a_ruled_out_message_is_absent_from_ui_v1(name: str):
    """QuotaBurn moved to tj-lenn8u (tj-utqd77 Q2); no health or storage history; settings live in the browser.

    Args:
        name: A message the design rules out of phase 3.
    """
    found = _all_names()
    assert 'UsageSummary' in found, 'guard the guard: the sweep must see the phase 3 messages'
    assert name not in found


def test_ui_config_is_unchanged():
    """OPTION R item 5: no UiConfig change. The full list, so an appended field is red too."""
    assert [_shape(field) for field in messages(file_named(SHELL))['UiConfig'].field] == [
        _r('allowed_groups', 1, f'{UI}AccountGroup'),
        _f('deployment_label', 2, S),
        _f('server_version', 3, S),
    ]


def test_stats_imports_data_for_client_kind_and_nothing_internal():
    """ClientKind is declared once, in data.proto; stats.proto reaches it by import, never a second copy."""
    assert 'ClientKind' not in {enum.name for enum in file_named(STATS).enum_type}
    assert set(file_named(STATS).dependency) == {
        'google/protobuf/duration.proto',
        'google/protobuf/timestamp.proto',
        'trader_joe/proto/market/v1/enums.proto',
        f'{UI_DIR}/data.proto',
    }


# ---------------------------------------------------------------------------------------------------
# THE GENERATED MODULES, AND THE JSON THE UI RECEIVES

_PROBE = """
import json, sys

from google.protobuf import json_format
from trader_joe.proto.ui.v1 import data_pb2, stats_pb2

usage = data_pb2.DatasetUsage(
    reads_30d=2**40,
    read_by_30d=[data_pb2.CLIENT_KIND_UI],
    read_by_clients_30d=['operator'],
    client_kinds=[data_pb2.CLIENT_KIND_SDK],
)
usage.last_read_at.FromJsonString('2026-10-13T20:30:00Z')
summary = data_pb2.DatasetSummary(id='d-1', usage=usage, reaper_pending=True)
facets = data_pb2.DatasetFacets(
    clients=[data_pb2.ClientCount(client_id='_unattributed', client_kind=data_pb2.CLIENT_KIND_SDK, datasets=4)],
    client_ids=['research'],
    stale=5,
    read_30d=6,
    never_read=7,
    expiring_soon=8,
    stale_days=90,
)
facets.expires_before.FromJsonString('2026-10-21T00:00:00Z')
override = data_pb2.OperatorOverride(reason='owner asked', confirmed=True)
clear = data_pb2.SetExpiryRequest(override=override)
set_to = data_pb2.SetExpiryRequest()
set_to.expiry.FromJsonString('2026-11-01T00:00:00Z')
pending = data_pb2.ReaperPendingPage(items=[summary], deletes_enabled=False)
pending.as_of.FromJsonString('2026-10-14T03:00:00Z')
open_bucket = stats_pb2.LastReadBucket(min_days=90, datasets=3)
zero_bucket = stats_pb2.LastReadBucket(min_days=0, max_days=0, datasets=1)
reads = stats_pb2.ReadsPerDay(
    days=[stats_pb2.ReadsPerDayEntry(day='2026-10-13', client_id='research', client_kind=data_pb2.CLIENT_KIND_SDK, reads=9)],
    client_kinds=[data_pb2.CLIENT_KIND_SDK],
)
storage = stats_pb2.StorageCurrent(total_bytes=2**33)
never = stats_pb2.ClientLastRead(client_id='research', client_kind=data_pb2.CLIENT_KIND_SDK)


def round_trip(message):
    return json_format.Parse(json_format.MessageToJson(message), type(message)()) == message


every = (summary, facets, clear, set_to, pending, open_bucket, zero_bucket, reads, storage, never)
json.dump(
    {
        'files_from': sorted(module.__file__ for module in (data_pb2, stats_pb2)),
        'summary': json_format.MessageToDict(summary),
        'facets': json_format.MessageToDict(facets),
        'clear': json_format.MessageToDict(clear),
        'clear_has_expiry': clear.HasField('expiry'),
        'set_to': json_format.MessageToDict(set_to),
        'delete': json_format.MessageToDict(data_pb2.DeleteDatasetRequest(override=override)),
        'pending': json_format.MessageToDict(pending),
        'open_bucket': json_format.MessageToDict(open_bucket),
        'zero_bucket': json_format.MessageToDict(zero_bucket),
        'reads': json_format.MessageToDict(reads),
        'storage': json_format.MessageToDict(storage),
        'never': json_format.MessageToDict(never),
        'round_trips': [round_trip(m) for m in every],
    },
    sys.stdout,
)
"""


@cache
def _probe() -> dict:
    """Import the phase 3 ui/v1 messages from gen/ alone and render them as canonical JSON."""
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


def test_the_phase_three_messages_generate_and_import_from_gen_alone():
    seen = _probe()
    assert all(Path(path).is_relative_to(GENERATED_ROOT) for path in seen['files_from']), seen['files_from']
    assert all(seen['round_trips']), 'a phase 3 message does not parse back from its canonical JSON'


def test_dataset_usage_and_facets_render_as_the_canonical_json_the_ui_reads():
    """int64 as a string, enum NAMES, RFC 3339 'Z', lowerCamelCase keys (decision tj-grna9p.4 section 2)."""
    seen = _probe()
    usage = {
        'lastReadAt': '2026-10-13T20:30:00Z',
        'reads30d': '1099511627776',
        'readBy30d': ['CLIENT_KIND_UI'],
        'readByClients30d': ['operator'],
        'clientKinds': ['CLIENT_KIND_SDK'],
    }
    assert seen['summary'] == {'id': 'd-1', 'usage': usage, 'reaperPending': True}
    assert seen['facets'] == {
        'clients': [{'clientId': '_unattributed', 'clientKind': 'CLIENT_KIND_SDK', 'datasets': '4'}],
        'clientIds': ['research'],
        'stale': '5',
        'read30d': '6',
        'neverRead': '7',
        'expiringSoon': '8',
        'staleDays': 90,
        'expiresBefore': '2026-10-21T00:00:00Z',
    }


def test_an_unset_expiry_is_absent_so_it_reads_as_clear_not_as_the_epoch():
    """SetExpiryRequest.expiry is `optional`: the body without it clears the expiry; with it, sets it."""
    seen = _probe()
    override = {'reason': 'owner asked', 'confirmed': True}
    assert seen['clear'] == {'override': override}
    assert seen['clear_has_expiry'] is False
    assert seen['set_to'] == {'expiry': '2026-11-01T00:00:00Z'}
    assert seen['delete'] == {'override': override}


def test_the_reaper_page_and_the_usage_stats_render_as_canonical_json():
    """deletes_enabled false (dry-run) is the default and so absent; an open bucket has no maxDays, a 0 one has."""
    seen = _probe()
    assert seen['pending']['asOf'] == '2026-10-14T03:00:00Z'
    assert 'deletesEnabled' not in seen['pending']
    assert seen['pending']['items'][0]['reaperPending'] is True
    assert seen['open_bucket'] == {'minDays': 90, 'datasets': '3'}
    # min_days has no presence, so its 0 is omitted; max_days has, so its 0 is written out.
    assert seen['zero_bucket'] == {'maxDays': 0, 'datasets': '1'}
    assert seen['reads'] == {
        'days': [{'day': '2026-10-13', 'clientId': 'research', 'clientKind': 'CLIENT_KIND_SDK', 'reads': '9'}],
        'clientKinds': ['CLIENT_KIND_SDK'],
    }
    assert seen['storage'] == {'totalBytes': '8589934592'}
    assert seen['never'] == {'clientId': 'research', 'clientKind': 'CLIENT_KIND_SDK'}
