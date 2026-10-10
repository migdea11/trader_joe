"""ui/v1, phase 1: the messages the /ui/v1 read routes return (tj-grna9p.14; decision tj-grna9p.4).

Pinned here is only what the phase 1 consumers lean on: GET /ui/v1/datasets, /datasets/{id} and
/datasets/{id}/bars (tj-grna9p.20) and GET /ui/v1/config (tj-grna9p.45). Later phases add fields
additively; buf breaking guards the numbering, so these check presence and type, never a full list.

* THE GENERATED MODULES IMPORT. Nothing under gen/ is committed (user ruling 2026-10-05): `make proto`
  writes trader_joe.proto.ui.v1 and every pytest target runs it first. The import happens in a
  subprocess with only gen/proto/python on PYTHONPATH, because TID251 keeps the generated package
  out of everything but common/rpc, and a test is not that seam. The same subprocess renders the
  canonical JSON decision tj-grna9p.4 section 2 names: lowerCamelCase, enum NAMES, int64 as strings,
  Timestamp as RFC 3339 'Z' -- what the UI receives.
* THE FIELD CHOICES. Ids are strings (a UUID as text); the market vocabulary and Bar are imported
  from market/v1, never restated (tj-grna9p.4 section 1); counts are int64.
* OVERDUE, NOT STALE. The approved design uses Stale for unread datasets, so the missed-deadline
  freshness value was renamed (tj-grna9p.14 amendment of 2026-10-06, item 5).
* HALF-OPEN RANGES are documented on the range fields (tj-vhboky.1 addendum; tj-86g751), read from
  the compiler's own source info rather than from the text.
* NO CREDENTIAL MATERIAL crosses ui/v1 (tj-grna9p.14 acceptance).
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

from common.enums.data_stock import ExpiryType
from common.tests.proto_descriptors import PROTO_ROOT, PROTOC_TIMEOUT_S, file_named, messages
from common.tests.roots import REPO_ROOT


pytestmark = pytest.mark.common

GENERATED_ROOT = REPO_ROOT / 'gen' / 'proto' / 'python'
DATA = 'trader_joe/proto/ui/v1/data.proto'
SHELL = 'trader_joe/proto/ui/v1/shell.proto'
UI = '.trader_joe.proto.ui.v1.'
MARKET = '.trader_joe.proto.market.v1.'
TIMESTAMP = '.google.protobuf.Timestamp'
FIELD = descriptor_pb2.FieldDescriptorProto
# FileDescriptorProto.message_type and DescriptorProto.field, as SourceCodeInfo paths number them.
MESSAGE_TYPE_PATH, FIELD_PATH, SYNTAX_PATH = 4, 2, 12
HALF_OPEN = '[start, end)'
CREDENTIAL = re.compile(r'secret|token|password|passwd|credential|api_?key|private_?key|bearer', re.IGNORECASE)


def _ran(result: subprocess.CompletedProcess) -> str:
    return f'exit {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}'


# ---------------------------------------------------------------------------------------------------
# THE GENERATED MODULES, AND THE JSON THE UI RECEIVES

_UI_PROBE = """
import json, sys

from google.protobuf import json_format
from trader_joe.proto.market.v1 import bar_pb2, enums_pb2
from trader_joe.proto.ui.v1 import data_pb2, shell_pb2

summary = data_pb2.DatasetSummary(
    id='1b4e28ba-2fa1-11d2-883f-0016d3cca427',
    granularity=enums_pb2.GRANULARITY_ONE_DAY,
    bar_count=2**40,
    freshness=data_pb2.Freshness(status=data_pb2.FRESHNESS_STATUS_OVERDUE),
)
summary.start.FromJsonString('2026-01-02T00:00:00Z')
summary.end.FromJsonString('2026-01-03T00:00:00Z')
page = data_pb2.DatasetPage(items=[summary], next_cursor='after-1')
bars = data_pb2.BarPage(bars=[bar_pb2.Bar(open=1.5)], next_cursor='')
facets = data_pb2.DatasetFacets(all=3, needs_attention=1)
config = shell_pb2.UiConfig(
    allowed_groups=[shell_pb2.ACCOUNT_GROUP_SIMULATION], deployment_label='dev', server_version='0.1.0'
)
json.dump(
    {
        'files': [data_pb2.DESCRIPTOR.name, shell_pb2.DESCRIPTOR.name],
        'data_file': data_pb2.__file__,
        'page': json_format.MessageToDict(page),
        'page_round_trip': json_format.Parse(json_format.MessageToJson(page), data_pb2.DatasetPage()) == page,
        'bars': json_format.MessageToDict(bars),
        'facets': json_format.MessageToDict(facets),
        'config': json_format.MessageToDict(config),
    },
    sys.stdout,
)
"""


@cache
def _ui_probe() -> dict:
    """Import the generated ui/v1 modules from gen/ alone and render each phase 1 message as canonical JSON."""
    assert (GENERATED_ROOT / 'trader_joe' / 'proto' / 'ui' / 'v1').is_dir(), (
        f'{GENERATED_ROOT} holds no ui/v1 package: make proto did not generate it'
    )
    env = {**os.environ, 'PYTHONPATH': str(GENERATED_ROOT), 'PYTHONDONTWRITEBYTECODE': '1'}
    with tempfile.TemporaryDirectory() as cwd:
        result = subprocess.run(
            [sys.executable, '-P', '-c', _UI_PROBE],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    assert result.returncode == 0, _ran(result)
    return json.loads(result.stdout)


def test_make_proto_generates_ui_v1_and_it_imports_from_gen_alone():
    """trader_joe.proto.ui.v1 comes from gen/, under its canonical descriptor names, beside market/v1."""
    seen = _ui_probe()
    assert seen['files'] == [DATA, SHELL]
    assert Path(seen['data_file']).is_relative_to(GENERATED_ROOT), seen['data_file']


def test_a_dataset_page_renders_as_the_canonical_json_the_ui_reads():
    """tj-grna9p.4 section 2: string id, int64 as a string, enum names, RFC 3339 'Z', lowerCamelCase.

    This is the body GET /ui/v1/datasets returns (tj-grna9p.20). bar_count is past 2**32 so an int32
    field would be visible here as well as in the descriptor test below.
    """
    seen = _ui_probe()
    assert seen['page'] == {
        'items': [
            {
                'id': '1b4e28ba-2fa1-11d2-883f-0016d3cca427',
                'granularity': 'GRANULARITY_ONE_DAY',
                'start': '2026-01-02T00:00:00Z',
                'end': '2026-01-03T00:00:00Z',
                'barCount': '1099511627776',
                'freshness': {'status': 'FRESHNESS_STATUS_OVERDUE'},
            }
        ],
        'nextCursor': 'after-1',
    }
    assert seen['page_round_trip'], 'the canonical JSON does not parse back to the same DatasetPage'


def test_bar_pages_facets_and_config_render_as_canonical_json():
    """BarPage carries market/v1 Bars; facet counts are int64 strings; UiConfig names its groups."""
    seen = _ui_probe()
    assert seen['bars'] == {'bars': [{'open': 1.5}]}, 'an empty next_cursor is the last page, omitted'
    assert seen['facets'] == {'all': '3', 'needsAttention': '1'}
    assert seen['config'] == {
        'allowedGroups': ['ACCOUNT_GROUP_SIMULATION'],
        'deploymentLabel': 'dev',
        'serverVersion': '0.1.0',
    }


# ---------------------------------------------------------------------------------------------------
# THE FIELD CHOICES


def _fields(file: str, message: str) -> dict[str, descriptor_pb2.FieldDescriptorProto]:
    return {field.name: field for field in messages(file_named(file))[message].field}


def _kind(field: descriptor_pb2.FieldDescriptorProto) -> tuple[str, str, bool]:
    """(scalar type or the referenced type's full name, its kind, repeated) for one field."""
    named = field.type in (FIELD.TYPE_MESSAGE, FIELD.TYPE_ENUM)
    return (
        field.type_name if named else FIELD.Type.Name(field.type),
        FIELD.Type.Name(field.type),
        field.label == FIELD.LABEL_REPEATED,
    )


def _enum(name: str) -> tuple[str, str, bool]:
    return (name, 'TYPE_ENUM', False)


def _message(name: str, repeated: bool = False) -> tuple[str, str, bool]:
    return (name, 'TYPE_MESSAGE', repeated)


def _scalar(type_name: str) -> tuple[str, str, bool]:
    return (type_name, type_name, False)


# What tj-grna9p.20 maps a store dataset onto, and what tj-grna9p.45 fills. A subset per message:
# later phases add fields, and a field added is not a field these consumers lose.
PHASE_ONE = {
    (DATA, 'DatasetSummary'): {
        'id': _scalar('TYPE_STRING'),
        'asset_symbol': _scalar('TYPE_STRING'),
        'asset_type': _enum(f'{MARKET}AssetType'),
        'data_type': _enum(f'{MARKET}DataType'),
        'source': _enum(f'{MARKET}DataSource'),
        'feed': _enum(f'{MARKET}Feed'),
        'granularity': _enum(f'{MARKET}Granularity'),
        'start': _message(TIMESTAMP),
        'end': _message(TIMESTAMP),
        'update_type': _enum(f'{MARKET}UpdateType'),
        'expiry_type': _enum(f'{UI}ExpiryType'),
        'expiry': _message(TIMESTAMP),
        'owner': _scalar('TYPE_STRING'),
        'state': _enum(f'{UI}DatasetState'),
        'first_bar': _message(TIMESTAMP),
        'last_bar': _message(TIMESTAMP),
        'bar_count': _scalar('TYPE_INT64'),
        'freshness': _message(f'{UI}Freshness'),
        'siblings': _message(f'{UI}DatasetSibling', repeated=True),
    },
    (DATA, 'DatasetSibling'): {'id': _scalar('TYPE_STRING'), 'granularity': _enum(f'{MARKET}Granularity')},
    (DATA, 'Freshness'): {
        'status': _enum(f'{UI}FreshnessStatus'),
        'expected_last_bar': _message(TIMESTAMP),
        'gap_count': _scalar('TYPE_INT32'),
        'as_of': _message(TIMESTAMP),
    },
    (DATA, 'DatasetPage'): {
        'items': _message(f'{UI}DatasetSummary', repeated=True),
        'next_cursor': _scalar('TYPE_STRING'),
    },
    (DATA, 'BarPage'): {'bars': _message(f'{MARKET}Bar', repeated=True), 'next_cursor': _scalar('TYPE_STRING')},
    (DATA, 'DatasetFacets'): {
        'all': _scalar('TYPE_INT64'),
        'needs_attention': _scalar('TYPE_INT64'),
        'sources': _message(f'{UI}SourceFacet', repeated=True),
        'update_types': _message(f'{UI}UpdateTypeFacet', repeated=True),
        'statuses': _message(f'{UI}StatusFacet', repeated=True),
    },
    (SHELL, 'UiConfig'): {
        'allowed_groups': (f'{UI}AccountGroup', 'TYPE_ENUM', True),
        'deployment_label': _scalar('TYPE_STRING'),
        'server_version': _scalar('TYPE_STRING'),
    },
}


@pytest.mark.parametrize(('file', 'message'), sorted(PHASE_ONE), ids=[message for _, message in sorted(PHASE_ONE)])
def test_each_phase_one_message_carries_the_fields_its_consumers_map(file: str, message: str):
    """Name, type and cardinality of every field tj-grna9p.20 and .45 read or write, per message."""
    fields = _fields(file, message)
    expected = PHASE_ONE[(file, message)]
    missing = sorted(set(expected) - set(fields))
    assert not missing, f'{message} lacks {missing}'
    actual = {name: _kind(fields[name]) for name in expected}
    assert actual == expected


def test_ui_v1_imports_the_market_vocabulary_and_restates_none_of_it():
    """tj-grna9p.4 section 1: Bar and the enums come from market/v1; ui/v1 declares no enum of the same name."""
    data = file_named(DATA)
    assert data.package == 'trader_joe.proto.ui.v1'
    assert set(data.dependency) == {
        'google/protobuf/timestamp.proto',
        'trader_joe/proto/market/v1/bar.proto',
        'trader_joe/proto/market/v1/enums.proto',
    }
    market = {enum.name for enum in file_named('trader_joe/proto/market/v1/enums.proto').enum_type}
    restated = [enum.name for file in (DATA, SHELL) for enum in file_named(file).enum_type if enum.name in market]
    assert not restated, f'ui/v1 restates market/v1 vocabulary: {restated}'
    assert 'Bar' not in messages(data)


def _values(file: str, enum: str) -> list[str]:
    return [value.name for value in {e.name: e for e in file_named(file).enum_type}[enum].value]


def test_freshness_names_a_missed_deadline_overdue_and_nothing_stale():
    """The 2026-10-06 rename: OVERDUE is the failure value; no freshness value is called STALE."""
    assert _values(DATA, 'FreshnessStatus') == [
        'FRESHNESS_STATUS_UNSPECIFIED',
        'FRESHNESS_STATUS_FRESH',
        'FRESHNESS_STATUS_LATE',
        'FRESHNESS_STATUS_OVERDUE',
        'FRESHNESS_STATUS_COMPLETE',
        'FRESHNESS_STATUS_GAPS',
        'FRESHNESS_STATUS_RETIRED',
    ]
    stale = [value for value in _values(DATA, 'FreshnessStatus') if 'STALE' in value]
    assert not stale, stale


def test_expiry_type_mirrors_common_enums_by_name():
    """ExpiryType is defined in ui/v1 and mirrors common.enums.data_stock.ExpiryType by member name.

    tj-grna9p.20 maps a store dataset's ExpiryType onto it by name, the rule market/v1 follows too
    (test_proto_market_vocabulary.py); a member on one side only is a dataset the route cannot render.
    """
    values = _values(DATA, 'ExpiryType')
    assert values[0] == 'EXPIRY_TYPE_UNSPECIFIED'
    assert {value.removeprefix('EXPIRY_TYPE_') for value in values[1:]} == set(ExpiryType.__members__)


def test_ui_config_can_say_simulation():
    """tj-grna9p.45 serves allowed_groups [SIMULATION] until PR 5."""
    assert 'ACCOUNT_GROUP_SIMULATION' in _values(SHELL, 'AccountGroup')


def test_no_ui_v1_field_or_message_names_credential_material():
    """tj-grna9p.14 acceptance: nothing crossing ui/v1 carries a secret, a token or a key."""
    named = [
        f'{message.name}.{field.name}'
        for file in (DATA, SHELL)
        for message in file_named(file).message_type
        for field in message.field
        if CREDENTIAL.search(field.name) or CREDENTIAL.search(message.name)
    ]
    assert not named, named


# ---------------------------------------------------------------------------------------------------
# HALF-OPEN RANGES, AS DOCUMENTED


@cache
def _data_with_source_info() -> descriptor_pb2.FileDescriptorProto:
    """data.proto compiled with its comments: the compiler's own reading of what documents what."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / 'data.bin'
        result = subprocess.run(
            [
                sys.executable,
                '-m',
                'grpc_tools.protoc',
                f'-I{PROTO_ROOT}',
                f'--descriptor_set_out={out}',
                '--include_source_info',
                DATA,
            ],
            capture_output=True,
            text=True,
            timeout=PROTOC_TIMEOUT_S,
            check=False,
        )
        assert result.returncode == 0, _ran(result)
        parsed = descriptor_pb2.FileDescriptorSet()
        parsed.ParseFromString(out.read_bytes())
    (file,) = parsed.file
    return file


def _comments(path: list[int]) -> str:
    """Every comment the compiler attaches at PATH: detached, leading and trailing, joined."""
    file = _data_with_source_info()
    found = [location for location in file.source_code_info.location if list(location.path) == path]
    assert found, f'no source location at {path}'
    location = found[0]
    return ' '.join([*location.leading_detached_comments, location.leading_comments, location.trailing_comments])


def _message_path(name: str) -> list[int]:
    names = [message.name for message in _data_with_source_info().message_type]
    return [MESSAGE_TYPE_PATH, names.index(name)]


def _says_half_open(text: str) -> bool:
    return 'half-open' in text.lower() and HALF_OPEN in text


def test_the_file_declares_every_range_half_open():
    """The header: every range in data.proto is [start, end)."""
    header = _comments([SYNTAX_PATH])
    assert _says_half_open(header), header


def test_the_dataset_range_is_documented_half_open():
    """DatasetSummary.start and end: the requested range, start included, end excluded (tj-86g751)."""
    path = _message_path('DatasetSummary')
    names = [field.name for field in _data_with_source_info().message_type[path[1]].field]
    text = _comments([*path, FIELD_PATH, names.index('start')])
    assert _says_half_open(text), text
    assert 'end excluded' in text, text


def test_a_bar_page_is_documented_within_a_half_open_range():
    """BarPage: the bars route reads [start, end) (tj-grna9p.20), and the message says so."""
    text = _comments(_message_path('BarPage'))
    assert _says_half_open(text), text
