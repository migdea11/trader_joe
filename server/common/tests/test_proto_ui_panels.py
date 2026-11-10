"""ui/v1: the panel contracts (tj-7zjov1.35, P23; ADR tj-26jt9w addendum 20 and the owner rulings of 2026-10-27).

The design is the bead's acceptance and the architect's notes on it (13:50 UTC 2026-10-27, panel rulings; 03:31 UTC
2026-10-28, the vocabulary as P20 landed it), with the owner rulings on the epic tj-7zjov1:

* the name is PANEL: no identifier in the file says world or universe;
* panels are UI-only (owner Q5): the file is messages and enums, no service;
* the enums mirror the store's PanelCadence (DAILY, WEEKLY, MONTHLY), PanelTransition and PanelPassKind by member
  NAME, after an UNSPECIFIED zero; the store-side half of the mirror is server/data/store/tests/
  test_panel_enums_mirror_ui_proto.py, since this suite cannot import the store;
* settings split panel (enabled, cadence, filters) / source (ramp_datasets_per_hour, request_cap_per_hour,
  corporate_action_window_pre_days, corporate_action_window_post_days), the per-hour unit KEPT IN THE FIELD NAME
  because it is a reversible assumption;
* settings versions carry version_no, saved_at, actor, reason and the derived effective pass (unset when none);
* enable and disable are a settings version: the same write, no separate body;
* every write carries a reason field, and the reason is OPTIONAL (owner ruling 2026-11-06, decision tj-46jkey section
  2): a plain string field (no presence, so absent and blank are the same value, meaning none) whose comment says it
  is optional and never refused; the version and marker reads say an empty reason means none was given;
* a panel has NO SIZE (owner ruling 2026-11-06, decision tj-46jkey section 1): PanelSettings,
  SavePanelSettingsRequest and CreatePanelRequest reserve field 3 and the name size, so neither can be reused and an
  old sender's field 3 never parses as a value;
* panel create is RULED (owner, 2026-10-27; the orchestrator correction of 2026-10-28 on tj-7zjov1.35): a UI write
  with an optional reason, and still no client-facing panel API. P23b (tj-7zjov1.199) adds it ADDITIVELY as one flat
  body, CreatePanelRequest: fields 1-9 (3 reserved) identical to SavePanelSettingsRequest 1-9 (cadence UNSPECIFIED
  excepted: the store applies MONTHLY), reason 10, name 11, source 12 and the optional first source settings 13-16;
  the response is the existing PanelDetail, so no response message is added. The same bead fixes the changed_count
  (F3) and day-to-instant (F7) comments of the P23 architect gate;
* INCLUDE OTC (owner ruling 2026-11-10, ADR tj-ed4t8m D2 and D4): one more panel setting, a PLAIN bool (implicit
  presence: absent is false, the ruled default, under which OTC securities are not selected), numbered
  PanelSettings 10, SavePanelSettingsRequest 11 (after the reason, which already held 10) and CreatePanelRequest 17
  (after the first source settings); the two writes document that false with exchange OTC is refused (422). Field 3
  and the name size stay reserved.

Every message is pinned as its full field list (name, number, type, cardinality, presence), and the file's message
and enum inventory as an exact set, because the file is new: an extra is a design change, not an append. Descriptors
come from protoc (common/tests/proto_descriptors.py), comments from protoc's source info, and the canonical JSON from
gen/ alone in a subprocess, for the reason test_proto_ui_phase_two.py gives (TID251).
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
PANELS = f'{UI_DIR}/panels.proto'
FIELD = descriptor_pb2.FieldDescriptorProto
MESSAGE_TYPE_PATH, FIELD_PATH, ENUM_TYPE_PATH, ENUM_VALUE_PATH, SYNTAX_PATH = 4, 2, 5, 2, 12
HALF_OPEN = '[start, end)'
# The same as test_proto_ui_phase_two.py, restated rather than imported from a test module.
CREDENTIAL = re.compile(r'secret|token|password|passwd|credential|api_?key|private_?key|bearer', re.IGNORECASE)
RETIRED_NAMES = re.compile(r'world|universe', re.IGNORECASE)

T, S, I64, I32, B = '.google.protobuf.Timestamp', 'TYPE_STRING', 'TYPE_INT64', 'TYPE_INT32', 'TYPE_BOOL'
M, UI = '.trader_joe.proto.market.v1.', '.trader_joe.proto.ui.v1.'


def _ran(result: subprocess.CompletedProcess) -> str:
    return f'exit {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}'


def _shape(field: descriptor_pb2.FieldDescriptorProto) -> tuple:
    """(name, number, type, repeated, `optional`), as test_proto_ui_phase_three.py reads a field."""
    kind = field.type_name if field.type in (FIELD.TYPE_MESSAGE, FIELD.TYPE_ENUM) else FIELD.Type.Name(field.type)
    return (field.name, field.number, kind, field.label == FIELD.LABEL_REPEATED, field.proto3_optional)


def _f(name: str, number: int, kind: str, repeated: bool = False, optional: bool = False) -> tuple:
    return (name, number, kind, repeated, optional)


def _r(name: str, number: int, kind: str) -> tuple:
    return _f(name, number, kind, repeated=True)


def _o(name: str, number: int, kind: str) -> tuple:
    return _f(name, number, kind, optional=True)


def _fields(message: str) -> dict[str, descriptor_pb2.FieldDescriptorProto]:
    return {field.name: field for field in messages(file_named(PANELS))[message].field}


# ---------------------------------------------------------------------------------------------------
# THE ENUMS


def _enum(enum: str) -> list[tuple[str, int]]:
    return [(value.name, value.number) for value in {e.name: e for e in file_named(PANELS).enum_type}[enum].value]


ENUMS = {
    # Mirrors the store's PanelCadence (owner ruling 2026-10-27: per panel, DAILY, WEEKLY, MONTHLY).
    'PanelCadence': [
        ('PANEL_CADENCE_UNSPECIFIED', 0),
        ('PANEL_CADENCE_DAILY', 1),
        ('PANEL_CADENCE_WEEKLY', 2),
        ('PANEL_CADENCE_MONTHLY', 3),
    ],
    # Mirrors the store's PanelTransition (addendum 20 (2), as landed by P20).
    'PanelTransition': [
        ('PANEL_TRANSITION_UNSPECIFIED', 0),
        ('PANEL_TRANSITION_ENTERED', 1),
        ('PANEL_TRANSITION_CREATED', 2),
        ('PANEL_TRANSITION_DEFERRED', 3),
        ('PANEL_TRANSITION_REFUSED', 4),
        ('PANEL_TRANSITION_EXITED', 5),
        ('PANEL_TRANSITION_DRAINING', 6),
        ('PANEL_TRANSITION_DRAINED', 7),
    ],
    # Mirrors the store's PanelPassKind (P20c 14:16 note: an effective pass may be of either kind).
    'PanelPassKind': [
        ('PANEL_PASS_KIND_UNSPECIFIED', 0),
        ('PANEL_PASS_KIND_MEMBERSHIP', 1),
        ('PANEL_PASS_KIND_RELEASE', 2),
    ],
    # Addendum 20 note (5): saved, missing, empty, not due.
    'AssetListDayState': [
        ('ASSET_LIST_DAY_STATE_UNSPECIFIED', 0),
        ('ASSET_LIST_DAY_STATE_SAVED', 1),
        ('ASSET_LIST_DAY_STATE_MISSING', 2),
        ('ASSET_LIST_DAY_STATE_EMPTY', 3),
        ('ASSET_LIST_DAY_STATE_NOT_DUE', 4),
    ],
    # Addendum 20 note (3): panel and source settings versions, operator collect stops and resumes.
    'PanelMarkerKind': [
        ('PANEL_MARKER_KIND_UNSPECIFIED', 0),
        ('PANEL_MARKER_KIND_PANEL_SETTINGS', 1),
        ('PANEL_MARKER_KIND_SOURCE_SETTINGS', 2),
        ('PANEL_MARKER_KIND_COLLECT_STOP', 3),
        ('PANEL_MARKER_KIND_COLLECT_RESUME', 4),
    ],
}


def test_the_file_declares_exactly_the_ruled_enums():
    assert sorted(enum.name for enum in file_named(PANELS).enum_type) == sorted(ENUMS)


@pytest.mark.parametrize('enum', sorted(ENUMS))
def test_each_enum_names_exactly_the_ruled_values_and_numbers(enum: str):
    """Names and numbers, in order: a renumbering or a dropped value is a wire break.

    Args:
        enum: An enum of panels.proto.
    """
    assert _enum(enum) == ENUMS[enum]


@pytest.mark.parametrize('enum', sorted(ENUMS))
def test_each_enum_opens_with_its_unspecified_zero(enum: str):
    """The ui/v1 convention: the zero value is <ENUM>_UNSPECIFIED, never a real member.

    Args:
        enum: An enum of panels.proto.
    """
    prefix = re.sub(r'(?<!^)(?=[A-Z])', '_', enum).upper()
    first = _enum(enum)[0]
    assert first == (f'{prefix}_UNSPECIFIED', 0), first
    assert all(name.startswith(f'{prefix}_') for name, _ in _enum(enum)), _enum(enum)


# ---------------------------------------------------------------------------------------------------
# THE MESSAGES, AS THE FULL FIELD LIST

PANEL_SETTINGS = [
    _f('enabled', 1, B),
    _f('cadence', 2, f'{UI}PanelCadence'),
    # 3 was size: reserved by number and name (owner ruling 2026-11-06).
    _r('allow_symbols', 4, S),
    _r('deny_symbols', 5, S),
    # The four below filter only when present: absent is no filter, so presence is the point.
    _o('asset_class', 6, f'{M}AssetType'),
    _o('exchange', 7, S),
    _o('active', 8, B),
    _o('tradable', 9, B),
    # ADR tj-ed4t8m D2: a PLAIN bool (no presence): absent is false, which is the ruled default (OTC not selected).
    _f('include_otc', 10, B),
]

SOURCE_SETTINGS = [
    # ADR tj-2ayia2 D3: the rates have EXPLICIT presence on the read: unset is NO LIMIT, and 0 never appears.
    _o('ramp_datasets_per_hour', 1, I32),
    _o('request_cap_per_hour', 2, I32),
    _f('corporate_action_window_pre_days', 3, I32),
    _f('corporate_action_window_post_days', 4, I32),
]

MESSAGES = {
    # (1) the selector.
    'PanelListItem': [_f('id', 1, S), _f('name', 2, S), _f('source', 3, f'{M}DataSource'), _f('enabled', 4, B)],
    'PanelList': [_r('items', 1, f'{UI}PanelListItem')],
    # (2) settings, the Q1 split.
    'PanelSettings': PANEL_SETTINGS,
    'PanelSourceSettings': SOURCE_SETTINGS,
    'EffectivePass': [_f('pass_id', 1, S), _f('kind', 2, f'{UI}PanelPassKind'), _f('started_at', 3, T)],
    'PanelSettingsVersion': [
        _f('version_no', 1, I32),
        _f('settings', 2, f'{UI}PanelSettings'),
        _f('saved_at', 3, T),
        _f('actor', 4, S),
        _f('reason', 5, S),
        _f('effective_pass', 6, f'{UI}EffectivePass'),
    ],
    'PanelSourceSettingsVersion': [
        _f('source', 1, f'{M}DataSource'),
        _f('version_no', 2, I32),
        _f('settings', 3, f'{UI}PanelSourceSettings'),
        _f('saved_at', 4, T),
        _f('actor', 5, S),
        _f('reason', 6, S),
        _f('effective_pass', 7, f'{UI}EffectivePass'),
    ],
    # (4) counts, int64 as every ui/v1 count of datasets is.
    'PanelMembershipCounts': [
        _f('members', 1, I64),
        _f('collecting', 2, I64),
        _f('draining', 3, I64),
        _f('lagging', 4, I64),
    ],
    'PanelDetail': [
        _f('id', 1, S),
        _f('name', 2, S),
        _f('source', 3, f'{M}DataSource'),
        _f('created_at', 4, T),
        _f('current_version', 5, f'{UI}PanelSettingsVersion'),
        _f('current_source_version', 6, f'{UI}PanelSourceSettingsVersion'),
        _f('counts', 7, f'{UI}PanelMembershipCounts'),
    ],
    'PanelSettingsVersionPage': [_r('items', 1, f'{UI}PanelSettingsVersion'), _f('next_cursor', 2, S)],
    'PanelSourceSettingsVersionPage': [_r('items', 1, f'{UI}PanelSourceSettingsVersion'), _f('next_cursor', 2, S)],
    # (3) the boundary read.
    'PanelMarker': [
        _f('kind', 1, f'{UI}PanelMarkerKind'),
        _f('at', 2, T),
        _f('version_no', 3, I32),
        _f('actor', 4, S),
        _f('reason', 5, S),
        _f('dataset_id', 6, S),
        _f('label', 7, S),
    ],
    'PanelBoundaries': [_f('start', 1, T), _f('end', 2, T), _r('markers', 3, f'{UI}PanelMarker')],
    # (5) the aggregate history, never per-asset rows.
    'AssetListHistoryDay': [_f('day', 1, S), _f('state', 2, f'{UI}AssetListDayState'), _f('changed_count', 3, I32)],
    'AssetListHistory': [
        _f('start', 1, T),
        _f('end', 2, T),
        _r('days', 3, f'{UI}AssetListHistoryDay'),
        _f('as_of', 4, T),
    ],
    # (6) member rows: vendor asset id and last observed symbol, never a listing id.
    'PanelMember': [
        _f('vendor_asset_id', 1, S),
        _f('symbol', 2, S),
        _f('status', 3, f'{UI}PanelTransition'),
        _f('cause', 4, S),
        _f('dataset_id', 5, S),
        _f('appeared_on', 6, S),
        _f('vanished_on', 7, S),
        _f('last_bar', 8, T),
        _f('health', 9, f'{UI}DatasetHealthStatus'),
        _f('as_of', 10, T),
        _f('recorded_at', 11, T),
    ],
    'PanelMemberPage': [_r('items', 1, f'{UI}PanelMember'), _f('next_cursor', 2, S)],
    # Acceptance: corporate-action events per dataset, the vendor's dates as the internal store contract has them.
    'CorporateActionEvent': [
        _f('vendor_action_id', 1, S),
        _f('action_type', 2, S),
        _f('ex_date', 3, T),
        _f('record_date', 4, T),
        _f('payable_date', 5, T),
        _f('process_date', 6, T),
        _f('effective_date', 7, T),
        _f('as_of', 8, T),
    ],
    'CorporateActionEventPage': [_r('items', 1, f'{UI}CorporateActionEvent'), _f('next_cursor', 2, S)],
    # (7) the writes. A presence-required value is `optional` so absent is refused, never read as false or 0.
    'SavePanelSettingsRequest': [
        _o('enabled', 1, B),
        _f('cadence', 2, f'{UI}PanelCadence'),
        # 3 was size: reserved.
        _r('allow_symbols', 4, S),
        _r('deny_symbols', 5, S),
        _o('asset_class', 6, f'{M}AssetType'),
        _o('exchange', 7, S),
        _o('active', 8, B),
        _o('tradable', 9, B),
        _f('reason', 10, S),
        # ADR tj-ed4t8m D2: after the reason, plain: a save without it turns OTC off (a save replaces as sent).
        _f('include_otc', 11, B),
    ],
    'SaveSourceSettingsRequest': [
        _o('ramp_datasets_per_hour', 1, I32),
        _o('request_cap_per_hour', 2, I32),
        _o('corporate_action_window_pre_days', 3, I32),
        _o('corporate_action_window_post_days', 4, I32),
        _f('reason', 5, S),
    ],
    # P23b: the create body, FLAT so presence survives; 1-9 as SavePanelSettingsRequest, then the create's own.
    'CreatePanelRequest': [
        _o('enabled', 1, B),
        _f('cadence', 2, f'{UI}PanelCadence'),
        # 3 was size: reserved.
        _r('allow_symbols', 4, S),
        _r('deny_symbols', 5, S),
        _o('asset_class', 6, f'{M}AssetType'),
        _o('exchange', 7, S),
        _o('active', 8, B),
        _o('tradable', 9, B),
        _f('reason', 10, S),
        _f('name', 11, S),
        _f('source', 12, f'{M}DataSource'),
        _o('ramp_datasets_per_hour', 13, I32),
        _o('request_cap_per_hour', 14, I32),
        _o('corporate_action_window_pre_days', 15, I32),
        _o('corporate_action_window_post_days', 16, I32),
        # ADR tj-ed4t8m D2: a panel setting numbered after the source settings, plain: absent is off.
        _f('include_otc', 17, B),
    ],
}


def test_the_file_declares_exactly_the_ruled_messages():
    """The inventory: a message added beside these (a create response, a settings event) is a design change."""
    assert sorted(messages(file_named(PANELS))) == sorted(MESSAGES)


@pytest.mark.parametrize('message', sorted(MESSAGES))
def test_each_message_carries_exactly_the_ruled_fields(message: str):
    """Name, number, type, cardinality and presence, as the full list: the file is new, so an extra is a change.

    Args:
        message: A message of panels.proto.
    """
    assert [_shape(field) for field in messages(file_named(PANELS))[message].field] == MESSAGES[message]


def test_no_message_nests_a_type():
    """Every type is top level, so the inventory above is the whole file."""
    nested = {
        m.name: [n.name for n in m.nested_type] + [e.name for e in m.enum_type] for m in file_named(PANELS).message_type
    }
    assert not {name: inner for name, inner in nested.items() if inner}, nested


# ---------------------------------------------------------------------------------------------------
# NO SIZE: FIELD 3 AND THE NAME RESERVED (owner ruling 2026-11-06, decision tj-46jkey section 1)

SIZED_ONCE = ['CreatePanelRequest', 'PanelSettings', 'SavePanelSettingsRequest']


def test_no_message_declares_a_size_field():
    """A panel has no size: no message of the file, not only the three that had one, declares it again."""
    found = {name: [f.name for f in message.field] for name, message in messages(file_named(PANELS)).items()}
    assert 'enabled' in found['PanelSettings'], 'guard the guard: the sweep must see the settings fields'
    assert not {name: fields for name, fields in found.items() if 'size' in fields}, found


@pytest.mark.parametrize('message', SIZED_ONCE)
def test_each_once_sized_message_reserves_field_3_and_the_name_size(message: str):
    """Exactly 3 and exactly 'size': a field list that merely omits size would let a later edit reuse either.

    Args:
        message: A message that carried size before the owner ruling of 2026-11-06.
    """
    compiled = messages(file_named(PANELS))[message]
    assert [(r.start, r.end) for r in compiled.reserved_range] == [(3, 4)], compiled.reserved_range
    assert list(compiled.reserved_name) == ['size']
    assert 3 not in {field.number for field in compiled.field}


def _compile_with(message: str, line: str) -> subprocess.CompletedProcess:
    """Compile panels.proto with LINE added as the first field of MESSAGE, from a copy shadowing the real file."""
    source = (PROTO_ROOT / PANELS).read_text(encoding='utf-8')
    opener = f'message {message} {{\n'
    assert source.count(opener) == 1, f'{opener!r} must open exactly one message'
    with tempfile.TemporaryDirectory() as tmp:
        shadow = Path(tmp) / PANELS
        shadow.parent.mkdir(parents=True)
        shadow.write_text(source.replace(opener, f'{opener}  {line}\n'), encoding='utf-8')
        out = Path(tmp) / 'out.bin'
        return subprocess.run(
            [
                sys.executable,
                '-m',
                'grpc_tools.protoc',
                f'-I{tmp}',
                f'-I{PROTO_ROOT}',
                f'--descriptor_set_out={out}',
                PANELS,
            ],
            capture_output=True,
            text=True,
            timeout=PROTOC_TIMEOUT_S,
            check=False,
        )


@pytest.mark.parametrize('message', SIZED_ONCE)
def test_neither_field_3_nor_the_name_size_can_be_reused(message: str):
    """The compiler refuses both reuses; a field that is neither compiles, so the refusal is the reservation's.

    Args:
        message: A message that carried size before the owner ruling of 2026-11-06.
    """
    control = _compile_with(message, 'int32 reservation_probe = 99;')
    assert control.returncode == 0, _ran(control)
    by_number = _compile_with(message, 'int32 reservation_probe = 3;')
    assert by_number.returncode != 0 and 'reserved' in by_number.stderr, _ran(by_number)
    by_name = _compile_with(message, 'int32 size = 99;')
    assert by_name.returncode != 0 and 'reserved' in by_name.stderr, _ran(by_name)


# ---------------------------------------------------------------------------------------------------
# UI-ONLY, NAMED PANEL, ONE CREATE BODY, NOTHING SECRET


def test_the_file_declares_no_service():
    """Owner Q5: panels are UI-only, and ui/v1 is REST over canonical protobuf JSON; no RPC exists here."""
    assert list(file_named(PANELS).service) == []
    assert file_named(PANELS).package == 'trader_joe.proto.ui.v1'


def _identifiers() -> list[str]:
    compiled = file_named(PANELS)
    names = [compiled.name]
    for enum in compiled.enum_type:
        names += [enum.name, *(value.name for value in enum.value)]
    for message in compiled.message_type:
        names += [message.name, *(field.name for field in message.field), *(field.json_name for field in message.field)]
    return names


def test_no_identifier_says_world_or_universe():
    """Addendum 20 (3): the name is PANEL; world and universe are retired working names. Comments may cite them."""
    found = _identifiers()
    assert 'PanelDetail' in found, 'guard the guard: the sweep must see the file'
    assert not [name for name in found if RETIRED_NAMES.search(name)]


def test_the_identifier_sweeps_reach_the_create_body():
    """Guard the guard for P23b: the world/universe and credential sweeps read the create body's names too."""
    found = set(_identifiers())
    assert {'CreatePanelRequest', 'name', 'source', 'rampDatasetsPerHour', 'requestCapPerHour'} <= found, found


def test_panel_create_is_exactly_one_body_and_answers_with_panel_detail():
    """Owner ruling 2026-10-27 (A): one create body, CreatePanelRequest; its response is the existing PanelDetail.

    No other create or New message, and no response message at all: a CreatePanelResponse would be a second shape for
    what PanelDetail already says. The exact inventory above pins the same thing; this names the reason.
    """
    names = list(messages(file_named(PANELS)))
    assert [name for name in names if 'create' in name.lower() or name.startswith('New')] == ['CreatePanelRequest']
    assert not [name for name in names if name.endswith(('Response', 'Reply', 'Result'))], names
    assert 'PanelDetail' in names
    text = _comments(_path('CreatePanelRequest'))
    assert 'The RESPONSE is the created PanelDetail' in text, text


def test_no_identifier_names_credential_material():
    """The ui/v1 sweep, on this file: actor is the operator principal, a label, never a credential."""
    assert not [name for name in _identifiers() if CREDENTIAL.search(name)]


def test_the_file_imports_only_data_market_v1_and_the_timestamp():
    assert list(file_named(PANELS).dependency) == [
        'google/protobuf/timestamp.proto',
        'trader_joe/proto/market/v1/enums.proto',
        f'{UI_DIR}/data.proto',
    ]


def test_dataset_health_status_is_reused_from_data_proto_never_restated():
    """A member's health is data.proto's DatasetHealthStatus, so LATE and OVERDUE mean one thing in both views."""
    assert _fields('PanelMember')['health'].type_name == f'{UI}DatasetHealthStatus'
    assert 'DatasetHealthStatus' not in {enum.name for enum in file_named(PANELS).enum_type}


# ---------------------------------------------------------------------------------------------------
# THE UNIT IN THE FIELD NAME, AND THE READ AND WRITE SHAPES AGREE

UNIT_SUFFIXES = ('_per_hour', '_days')


@pytest.mark.parametrize('message', ['PanelSourceSettings', 'SaveSourceSettingsRequest', 'CreatePanelRequest'])
def test_every_source_number_carries_its_unit_in_its_name(message: str):
    """The per-hour unit is a reversible assumption; a unit-free name would let a switch change meaning silently.

    With size removed (owner ruling 2026-11-06) the create body carries no panel number, so every int32 is a source one.

    Args:
        message: The source settings read or write, or the create body that carries the first source settings.
    """
    fields = messages(file_named(PANELS))[message].field
    numbers = [field.name for field in fields if field.type == FIELD.TYPE_INT32]
    assert numbers == [
        'ramp_datasets_per_hour',
        'request_cap_per_hour',
        'corporate_action_window_pre_days',
        'corporate_action_window_post_days',
    ]
    assert all(name.endswith(UNIT_SUFFIXES) for name in numbers), numbers


def test_the_source_write_body_is_the_read_settings_plus_a_reason():
    """Same names, numbers and types as the read, then reason; only presence may differ (absent is refused).

    The reason takes the number after the read's last field.
    """
    read_fields = [_shape(field)[:4] for field in messages(file_named(PANELS))['PanelSourceSettings'].field]
    write_fields = [_shape(field)[:4] for field in messages(file_named(PANELS))['SaveSourceSettingsRequest'].field]
    assert write_fields[:-1] == read_fields
    assert write_fields[-1] == ('reason', read_fields[-1][1] + 1, S, False)


def test_the_panel_write_body_is_the_read_settings_with_the_reason_before_include_otc():
    """The read's 1-9 (3 reserved) by name, number and type, then reason 10, then the read's include_otc as 11.

    include_otc was added after the reason had taken 10 on the write (ADR tj-ed4t8m D2), so it is 10 on the read and
    11 on the write: the same name and type, plain on both, and the only field whose number differs. Only presence may
    differ on 1-9 (absent is refused).
    """
    read_fields = [_shape(field) for field in messages(file_named(PANELS))['PanelSettings'].field]
    write_fields = [_shape(field) for field in messages(file_named(PANELS))['SavePanelSettingsRequest'].field]
    assert [shape[:4] for shape in write_fields[:-2]] == [shape[:4] for shape in read_fields[:-1]]
    assert read_fields[-1] == ('include_otc', 10, B, False, False)
    assert write_fields[-2:] == [('reason', 10, S, False, False), ('include_otc', 11, B, False, False)]


@pytest.mark.parametrize(
    ('message', 'number'), [('PanelSettings', 10), ('SavePanelSettingsRequest', 11), ('CreatePanelRequest', 17)]
)
def test_include_otc_is_a_plain_bool_with_implicit_presence(message: str, number: int):
    """ADR tj-ed4t8m D2: no `optional`, so the generated class has no HasField for it and absent IS false (the default).

    Args:
        message: A message carrying the panel setting.
        number: Its field number there.
    """
    field = _fields(message)['include_otc']
    assert (field.number, field.type, field.label, field.proto3_optional) == (
        number,
        FIELD.TYPE_BOOL,
        FIELD.LABEL_OPTIONAL,
        False,
    )
    assert not field.HasField('oneof_index'), field


@pytest.mark.parametrize('message', ['PanelSettings', 'SavePanelSettingsRequest', 'CreatePanelRequest'])
def test_include_otc_is_documented_absent_false_and_off_excludes_otc(message: str):
    """The comment says the rule the plain bool carries: absent is false, and false means OTC is not selected.

    Args:
        message: A message carrying the panel setting.
    """
    text = _comments(_path(message, 'include_otc'))
    assert 'an absent value is false' in text, text
    assert 'false (the default) means OTC securities are not selected' in text, text


@pytest.mark.parametrize('message', ['SavePanelSettingsRequest', 'CreatePanelRequest'])
def test_the_writes_document_the_otc_contradiction_as_refused(message: str):
    """ADR tj-ed4t8m D4: false together with exchange OTC selects nothing, so both writes refuse it.

    Args:
        message: A panel settings write body.
    """
    text = _comments(_path(message, 'include_otc'))
    assert 'A false value with exchange OTC is refused (422)' in text, text


def test_the_create_body_1_to_9_are_the_settings_write_1_to_9_with_presence():
    """P23b (A): fields 1-9 IDENTICAL in name, number, type and presence to SavePanelSettingsRequest 1-9.

    3 is reserved in both (owner ruling 2026-11-06), so 1-9 is eight fields: 1, 2 and 4-9. Read from the descriptors,
    not from MESSAGES, so a change to either body that the other does not follow is red.
    """
    save = [_shape(f) for f in messages(file_named(PANELS))['SavePanelSettingsRequest'].field if f.number < 10]
    create = [_shape(f) for f in messages(file_named(PANELS))['CreatePanelRequest'].field if f.number < 10]
    assert [shape[1] for shape in save] == [1, 2, 4, 5, 6, 7, 8, 9], save
    assert create == save


def test_the_create_body_is_the_read_settings_plus_reason_name_source_and_the_first_source_settings():
    """Exactly the difference from the settings write: name and source, the source settings, then include_otc.

    1-9 are PanelSettings' 1-9 names, numbers and types (eight fields: 3 is reserved in both); 10 is the same plain
    reason; 11 name and 12 source are plain (presence-free, so blank and UNSPECIFIED are what the route refuses);
    13-16 are PanelSourceSettings 1-4 shifted by 12, each `optional`, because all four absent means 'not given'; 17 is
    PanelSettings' include_otc (10 there), the same name and plain bool (ADR tj-ed4t8m D2).
    """
    panel = [_shape(field) for field in messages(file_named(PANELS))['PanelSettings'].field]
    source = [_shape(field) for field in messages(file_named(PANELS))['PanelSourceSettings'].field]
    create = [_shape(field) for field in messages(file_named(PANELS))['CreatePanelRequest'].field]
    assert len(panel) == 9, panel
    assert [shape[:4] for shape in create[:8]] == [shape[:4] for shape in panel[:8]]
    assert create[8:11] == [
        ('reason', 10, S, False, False),
        ('name', 11, S, False, False),
        ('source', 12, f'{M}DataSource', False, False),
    ]
    assert create[11:15] == [(name, number + 12, kind, False, True) for name, number, kind, _, _ in source]
    assert panel[8] == ('include_otc', 10, B, False, False)
    assert create[15:] == [('include_otc', 17, B, False, False)]


# ---------------------------------------------------------------------------------------------------
# THE COMMENTS: REQUIRED REASONS, REFUSED ABSENCES, HALF-OPEN RANGES, THE DERIVED READS


@cache
def _with_source_info() -> descriptor_pb2.FileDescriptorProto:
    """panels.proto compiled with its comments: the compiler's own reading of what documents what."""
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
                PANELS,
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


def _comments(path: list[int]) -> str:
    """Every comment protoc attaches at PATH (detached, leading, trailing), joined and with whitespace collapsed."""
    found = [location for location in _with_source_info().source_code_info.location if list(location.path) == path]
    assert found, f'no source location at {path} in {PANELS}'
    location = found[0]
    text = ' '.join([*location.leading_detached_comments, location.leading_comments, location.trailing_comments])
    return ' '.join(text.split())


def _path(message: str, field: str | None = None) -> list[int]:
    found = list(_with_source_info().message_type)
    index = [m.name for m in found].index(message)
    if field is None:
        return [MESSAGE_TYPE_PATH, index]
    return [MESSAGE_TYPE_PATH, index, FIELD_PATH, [f.name for f in found[index].field].index(field)]


def _enum_path(enum: str) -> list[int]:
    return [ENUM_TYPE_PATH, [e.name for e in _with_source_info().enum_type].index(enum)]


WRITE_REASON = 'Optional: trimmed; blank or absent means none was given (never refused).'
READ_REASON = 'Optional: empty means none was given.'
REASONS = {
    'SavePanelSettingsRequest': WRITE_REASON,
    'SaveSourceSettingsRequest': WRITE_REASON,
    'CreatePanelRequest': WRITE_REASON,
    'PanelSettingsVersion': READ_REASON,
    'PanelSourceSettingsVersion': READ_REASON,
    'PanelMarker': READ_REASON,
}


def test_the_reason_sweep_reaches_every_message_with_a_reason():
    """Guard the guard: REASONS names every message of the file that has a reason field, no more and no fewer."""
    with_reason = [name for name, message in messages(file_named(PANELS)).items() if 'reason' in _fields(name)]
    assert sorted(with_reason) == sorted(REASONS)


@pytest.mark.parametrize('message', sorted(REASONS))
def test_each_reason_is_documented_optional_and_never_required(message: str):
    """Owner ruling 2026-11-06 (decision tj-46jkey section 2): a reason is optional on every write, empty means none.

    The type is unchanged, a plain string with no presence, so absent and blank are one value; the comment says
    optional and never says the old required, never-blank or refused-otherwise wording.

    Args:
        message: A write body, or a version or marker read that echoes the reason given.
    """
    field = _fields(message)['reason']
    assert field.type == FIELD.TYPE_STRING and not field.proto3_optional and field.label != FIELD.LABEL_REPEATED
    text = _comments(_path(message, 'reason'))
    assert REASONS[message] in text, text
    assert not re.search(r'required|never blank|not blank|refused \(422\) otherwise', text, re.IGNORECASE), text


@pytest.mark.parametrize(
    ('message', 'field', 'words'),
    [
        ('SavePanelSettingsRequest', 'enabled', 'an absent value is refused (422), never read as false'),
        ('SavePanelSettingsRequest', 'cadence', 'UNSPECIFIED is refused (422)'),
        # ADR tj-2ayia2: the RATES are no longer presence-required (absent is no limit, pinned below); the window
        # day counts of the source write still are.
        (
            'SaveSourceSettingsRequest',
            'ramp_datasets_per_hour',
            '3-4, the window day counts: both required (an absent one is refused (422)), not negative.',
        ),
        # P23b: the create body. enabled has no default on create either (size is removed, 2026-11-06).
        ('CreatePanelRequest', 'enabled', 'an absent value is refused (422), never read as false; there is no default'),
        ('CreatePanelRequest', 'name', 'Required, trimmed, unique; blank or already used is refused (422).'),
        ('CreatePanelRequest', 'source', "Required; UNSPECIFIED is refused (422). Fixed for the panel's life."),
    ],
)
def test_each_presence_required_value_documents_that_absence_is_refused(message: str, field: str, words: str):
    """The F1 lesson of SetCollectingRequest.collect: an empty or forgotten body must not read as a value.

    Args:
        message: A write body.
        field: The field whose absence the route refuses.
        words: The sentence that says so.
    """
    text = _comments(_path(message, field))
    assert words in text, text


def test_enable_and_disable_is_documented_as_the_settings_write():
    """Owner Q2: enable and disable is a settings version (a marker with actor and reason), not a bare flag."""
    text = _comments(_path('SavePanelSettingsRequest'))
    assert 'Enable and disable is the same write with enabled changed' in text, text
    assert 'The server stamps saved_at and actor' in text, text


def test_the_create_cadence_unspecified_means_the_monthly_default_and_the_save_still_refuses_it():
    """Owner ruling (c) 2026-10-27: a create that names no cadence gets MONTHLY; a settings save never defaults it."""
    create = _comments(_path('CreatePanelRequest', 'cadence'))
    assert 'UNSPECIFIED means not named: the store applies its default, MONTHLY (DEFAULT_PANEL_CADENCE).' in create, (
        create
    )
    assert 'refused' not in create, create
    save = _comments(_path('SavePanelSettingsRequest', 'cadence'))
    assert 'UNSPECIFIED is refused (422)' in save and 'MONTHLY' not in save, save


@pytest.mark.parametrize(
    'words',
    [
        '13-16: the FIRST source settings.',
        # ADR tj-2ayia2 D4: a create on a source with no version ALWAYS writes its version 1.
        "On a source with NO settings version, the create always writes that source's version 1 from whatever 13-16 "
        'carry: an absent rate is no limit,',
        "an absent window day count takes the store's ruled 90 (DEFAULT_CORPORATE_ACTION_WINDOW_DAYS)",
        'all four absent writes no limits and 90/90.',
        'A present ramp under 1, cap under 2 or negative window day count is refused (422).',
        'On a source that already has a version, any of 13-16 is refused (422) and nothing is written:',
        "a source's settings apply to every panel on that source, so they change only through their own write, "
        'POST /panel-sources/{source}/settings-versions',
    ],
)
def test_the_first_source_settings_document_the_given_rule(words: str):
    """P23b (A) 13-16 as REVERSED by ADR tj-2ayia2 (owner ruling 2026-11-07), and the refusal of tj-7zjov1.209.

    Settings given for a source that already has a version are refused (422), nothing written, and change only through
    the source's own write (tj-7zjov1.245); pinned on the first of the four.

    Args:
        words: One sentence of the rule.
    """
    text = _comments(_path('CreatePanelRequest', 'ramp_datasets_per_hour'))
    assert words in text, text


def test_no_rate_comment_keeps_the_reversed_ruling():
    """ADR tj-2ayia2 reverses 'ramp and cap are both required', 'the rates have no default' and '0 pauses creation'.

    Every comment on the three source-settings messages and their rate fields is swept for the retired wording.
    """
    retired = re.compile(
        r'0 pauses creation|ramp 0 pauses|ramp and cap are both required|the rates have no default|'
        r'a window without both rates|has no settings and none is given',
        re.IGNORECASE,
    )
    texts = []
    for message in ('PanelSourceSettings', 'SaveSourceSettingsRequest', 'CreatePanelRequest'):
        texts.append(_comments(_path(message)))
        texts.extend(
            _comments(_path(message, field))
            for field in ('ramp_datasets_per_hour', 'request_cap_per_hour')
            if field in _fields(message)
        )
    assert not [text for text in texts if retired.search(text)], texts


@pytest.mark.parametrize(
    ('message', 'field', 'words'),
    [
        ('PanelSourceSettings', None, 'The two rates are optional: UNSET MEANS NO LIMIT, and 0 never appears'),
        ('PanelSourceSettings', 'ramp_datasets_per_hour', '1 or more. Unset: no limit.'),
        ('PanelSourceSettings', 'request_cap_per_hour', '2 or more (each new panel dataset costs two requests).'),
        ('PanelSourceSettings', 'request_cap_per_hour', 'Unset: no limit.'),
        ('SaveSourceSettingsRequest', 'ramp_datasets_per_hour', 'an absent rate means NO LIMIT (never refused'),
        (
            'SaveSourceSettingsRequest',
            'ramp_datasets_per_hour',
            'A present ramp under 1 or cap under 2 (so a present 0) is refused (422); a value above int32 max fails the '
            'parse (422).',
        ),
    ],
)
def test_the_rates_are_documented_optional_unset_is_no_limit_and_the_minimums(message, field, words):
    """ADR tj-2ayia2 D1/D3: the read and the source write say unset is no limit, ramp >= 1, cap >= 2."""
    text = _comments(_path(message, field))
    assert words in text, text


def test_the_first_source_settings_no_longer_say_a_create_writes_the_next_version():
    """tj-7zjov1.209: create_panel never writes a source's next version, so the comment must not say it does."""
    text = _comments(_path('CreatePanelRequest', 'ramp_datasets_per_hour'))
    assert 'NEXT version' not in text, text
    assert 'in the same transaction' not in text, text


@pytest.mark.parametrize(
    'words',
    [
        'FLAT, because a nested PanelSettings cannot carry presence.',
        'Fields 1-9 (3 is reserved) are identical in name, number, type and presence to SavePanelSettingsRequest 1-9.',
        'The server stamps created_at, saved_at and actor; version 1 is written with the panel in one transaction.',
        'The RESPONSE is the created PanelDetail: current_version is version 1 with effective_pass unset, counts all zero.',
        'The FIRST PASS is not run by this write and is never in its response',
        'a panel created disabled gets its first pass on the first tick after it is enabled',
        "The UI shows 'first pass pending' from the unset effective_pass and re-reads the detail.",
        "Every refusal is INVALID_REQUEST, 422, with the store's message.",
    ],
)
def test_the_create_body_documents_its_response_its_first_pass_and_its_refusals(words: str):
    """P23b (A), the message comment: what the write does, what it answers, and what it leaves to the panel manager.

    Args:
        words: One sentence of the message comment.
    """
    text = _comments(_path('CreatePanelRequest'))
    assert words in text, text


def test_the_header_keeps_the_unit_states_ui_only_and_says_create_is_ruled():
    """P23b (B): create is a ruled UI write; its reason is optional (2026-11-06); still no client-facing panel API."""
    header = _comments([SYNTAX_PATH])
    assert 'UNITS ARE KEPT IN THE FIELD NAME' in header and 'reversible assumption' in header, header
    assert 'UI-ONLY' in header, header
    assert (
        'PANEL CREATE is RULED (owner, 2026-10-27): a UI write with an optional recorded reason, CreatePanelRequest'
        in header
    ), header
    assert 'whose response is the existing PanelDetail' in header, header
    assert 'There is still no client-facing panel API.' in header, header
    assert 'deliberately absent' not in header and 'Q7' not in header, header


def test_the_header_maps_every_day_string_to_the_source_calendar():
    """P23 gate F7: a day string is the source-calendar date of its instant, and a range includes a day by its midnight."""
    header = _comments([SYNTAX_PATH])
    assert (
        'Every day string (day, appeared_on, vanished_on) is the source-calendar date of the instant it derives from.'
        in header
    ), header
    assert (
        "A day D is included in a half-open [start, end) range iff D's local midnight in the source calendar's timezone "
        'falls in [start, end).' in header
    ), header
    start = _comments(_path('AssetListHistory', 'start'))
    assert (
        "A day string D is included iff D's local midnight in the source calendar's timezone falls in [start, end)."
        in start
    ), start


def _ranges() -> list[str]:
    return [m.name for m in _with_source_info().message_type if {'start', 'end'} <= {f.name for f in m.field}]


def test_the_range_sweep_finds_every_panel_range():
    """Guard the guard: the sweep below reaches both ranged messages, so 'none failed' means something."""
    assert sorted(_ranges()) == ['AssetListHistory', 'PanelBoundaries']


@pytest.mark.parametrize('message', ['AssetListHistory', 'PanelBoundaries'])
def test_every_range_is_documented_half_open(message: str):
    """The file header states the rule, and each [start, end) pair says so on its start field (tj-86g751).

    Args:
        message: A message with a start and an end.
    """
    header = _comments([SYNTAX_PATH])
    assert 'HALF-OPEN' in header and HALF_OPEN in header, header
    text = _comments(_path(message, 'start'))
    assert 'Half-open' in text and HALF_OPEN in text, text


def test_lagging_is_documented_as_late_or_overdue():
    """The builder's derivation, which the notes left open: lagging is DatasetHealthStatus LATE or OVERDUE."""
    text = _comments(_path('PanelMembershipCounts', 'lagging'))
    assert '(LATE or OVERDUE)' in text, text


def test_the_counts_are_documented_as_derived_from_the_ledgers_latest_state():
    text = _comments(_path('PanelMembershipCounts'))
    assert "from the ledger's latest state per member" in text, text


def test_the_effective_pass_is_documented_derived_and_unset_when_not_yet_effective():
    """Addendum 20 (4): effective-from is DERIVED, never a column; a version no pass names is not yet effective."""
    assert 'derived and never stored' in _comments(_path('EffectivePass')), _comments(_path('EffectivePass'))
    for message in ('PanelSettingsVersion', 'PanelSourceSettingsVersion'):
        text = _comments(_path(message, 'effective_pass'))
        assert 'Unset when no pass has run under this version yet' in text, text


def test_the_history_is_documented_aggregate_and_the_member_by_vendor_id():
    """Owner ruling on P25 (04:40 UTC): one entry per expected day, never per-asset rows; 04:50 note: no listing id."""
    history = _comments(_path('AssetListHistory'))
    assert 'one entry per expected day' in history and 'Never per-asset rows' in history, history
    member = _comments(_path('PanelMember'))
    assert 'vendor asset id' in member and 'never by a listing id' in member, member


def test_changed_count_is_documented_over_the_sources_whole_list():
    """P23 gate F3: distinct vendor asset ids with an asset-list event that day, over the SOURCE's list, not the panel's."""
    day = _comments(_path('AssetListHistoryDay', 'changed_count'))
    assert 'Distinct vendor asset ids with at least one asset-list event (APPEARED, VANISHED or STATUS_FLIP)' in day, (
        day
    )
    assert "from the source's snapshots dated that day" in day, day
    assert "over the source's whole asset list, not the panel's filtered slice" in day, day
    assert '0 when the day is not SAVED.' in day, day
    assert _fields('AssetListHistoryDay')['changed_count'].type == FIELD.TYPE_INT32, 'F2 ruled int32'


def test_the_cadence_zero_is_documented_as_refused():
    text = _comments(_enum_path('PanelCadence'))
    assert 'zero value is never a real cadence and the store refuses it' in text, text


# ---------------------------------------------------------------------------------------------------
# THE GENERATED MODULE, AND THE JSON THE UI RECEIVES AND SENDS

_PROBE = """
import json, sys

from google.protobuf import json_format
from trader_joe.proto.market.v1 import enums_pb2
from trader_joe.proto.ui.v1 import data_pb2, panels_pb2

effective = panels_pb2.EffectivePass(pass_id='p-1', kind=panels_pb2.PANEL_PASS_KIND_RELEASE)
effective.started_at.FromJsonString('2026-10-28T20:15:00Z')
settings = panels_pb2.PanelSettings(
    enabled=True,
    cadence=panels_pb2.PANEL_CADENCE_MONTHLY,
    deny_symbols=['XYZ'],
    tradable=False,
)
version = panels_pb2.PanelSettingsVersion(
    version_no=2, settings=settings, actor='operator', reason='owner asked', effective_pass=effective
)
version.saved_at.FromJsonString('2026-10-28T20:00:00Z')
pending = panels_pb2.PanelSettingsVersion(version_no=3, settings=settings, actor='operator', reason='later')
source = panels_pb2.PanelSourceSettingsVersion(
    source=enums_pb2.DATA_SOURCE_ALPACA_API,
    version_no=1,
    # ADR tj-2ayia2: the ramp is NULL in the store (no limit) and the mapping passes None to the constructor as it does.
    settings=panels_pb2.PanelSourceSettings(
        ramp_datasets_per_hour=None,
        request_cap_per_hour=200,
        corporate_action_window_pre_days=90,
        corporate_action_window_post_days=90,
    ),
    actor='operator',
    reason='seed',
)
zero_read = panels_pb2.PanelSourceSettings(ramp_datasets_per_hour=0, request_cap_per_hour=0)
detail = panels_pb2.PanelDetail(
    id='panel-1',
    name='us',
    source=enums_pb2.DATA_SOURCE_ALPACA_API,
    current_version=version,
    current_source_version=source,
    counts=panels_pb2.PanelMembershipCounts(members=2**40, collecting=3, draining=1, lagging=2),
)
member = panels_pb2.PanelMember(
    vendor_asset_id='v-1',
    symbol='AAPL',
    status=panels_pb2.PANEL_TRANSITION_DEFERRED,
    cause='ramp_rate',
    appeared_on='2026-10-01',
    health=data_pb2.DATASET_HEALTH_STATUS_LATE,
)
day = panels_pb2.AssetListHistoryDay(day='2026-10-27', state=panels_pb2.ASSET_LIST_DAY_STATE_SAVED, changed_count=4)
marker = panels_pb2.PanelMarker(
    kind=panels_pb2.PANEL_MARKER_KIND_COLLECT_STOP, actor='operator', reason='halted', dataset_id='d-1', label='AAPL 1d'
)
listing = panels_pb2.PanelList(items=[panels_pb2.PanelListItem(id='panel-1', name='us', enabled=False)])
disable = panels_pb2.SavePanelSettingsRequest(
    enabled=False, cadence=panels_pb2.PANEL_CADENCE_DAILY, reason='pause for the sitting'
)
# ADR tj-2ayia2: a present 0 still reaches the route as a VALUE (which the store refuses, 422), never as absent.
pause = panels_pb2.SaveSourceSettingsRequest(
    ramp_datasets_per_hour=0,
    request_cap_per_hour=0,
    corporate_action_window_pre_days=0,
    corporate_action_window_post_days=0,
    reason='pause',
)
# And no limit is the rates ABSENT: no key on the wire.
no_limit = panels_pb2.SaveSourceSettingsRequest(
    corporate_action_window_pre_days=0, corporate_action_window_post_days=0, reason='no limit'
)
empty_panel = json_format.Parse('{}', panels_pb2.SavePanelSettingsRequest())
empty_source = json_format.Parse('{}', panels_pb2.SaveSourceSettingsRequest())
create = panels_pb2.CreatePanelRequest(
    enabled=False, reason='new panel', name='us', source=enums_pb2.DATA_SOURCE_ALPACA_API
)
create_first = panels_pb2.CreatePanelRequest(
    enabled=True,
    cadence=panels_pb2.PANEL_CADENCE_WEEKLY,
    tradable=True,
    reason='first panel',
    name='us',
    source=enums_pb2.DATA_SOURCE_ALPACA_API,
    ramp_datasets_per_hour=1,
    request_cap_per_hour=200,
)
empty_create = json_format.Parse('{}', panels_pb2.CreatePanelRequest())
# An old sender's size: field 3, varint 500 (tag 0x18). It must never parse as a value.
OLD_SIZE = bytes([0x18, 0xF4, 0x03])


def old_size(cls):
    message = cls()
    message.ParseFromString(OLD_SIZE)
    try:
        json_format.Parse('{"size": 500}', cls())
        json_size = 'parsed'
    except json_format.ParseError:
        json_size = 'refused'
    return {
        'declares_size': 'size' in cls.DESCRIPTOR.fields_by_name,
        'number_3': cls.DESCRIPTOR.fields_by_number.get(3) is not None,
        'set_fields': [field.name for field, _ in message.ListFields()],
        'kept_unknown': message.SerializeToString() == OLD_SIZE,
        'json': json_format.MessageToDict(message),
        'json_size': json_size,
    }


def round_trip(message):
    return json_format.Parse(json_format.MessageToJson(message), type(message)()) == message


def include_otc(cls):
    try:
        cls().HasField('include_otc')
        has_field = 'answered'
    except ValueError:
        has_field = 'refused'
    return {
        'absent': json_format.Parse('{}', cls()).include_otc,
        'sent_true': json_format.Parse('{"includeOtc": true}', cls()).include_otc,
        'true_json': json_format.MessageToDict(cls(include_otc=True)),
        'false_json': json_format.MessageToDict(cls(include_otc=False)),
        'has_field': has_field,
    }


every = (detail, member, day, marker, listing, disable, pause, no_limit, pending, create, create_first, zero_read)
json.dump(
    {
        'files_from': [panels_pb2.__file__],
        'detail': json_format.MessageToDict(detail),
        'pending': json_format.MessageToDict(pending),
        'pending_has_effective': pending.HasField('effective_pass'),
        'member': json_format.MessageToDict(member),
        'day': json_format.MessageToDict(day),
        'marker': json_format.MessageToDict(marker),
        'listing': json_format.MessageToDict(listing),
        'disable': json_format.MessageToDict(disable),
        'pause': json_format.MessageToDict(pause),
        'no_limit': json_format.MessageToDict(no_limit),
        'source_rates_has': [
            source.settings.HasField('ramp_datasets_per_hour'),
            source.settings.HasField('request_cap_per_hour'),
        ],
        'zero_read': json_format.MessageToDict(zero_read),
        'empty_has': {
            name: empty_panel.HasField(name) for name in ('enabled', 'asset_class', 'exchange', 'active', 'tradable')
        }
        | {
            name: empty_source.HasField(name)
            for name in (
                'ramp_datasets_per_hour',
                'request_cap_per_hour',
                'corporate_action_window_pre_days',
                'corporate_action_window_post_days',
            )
        },
        'empty_reason': [empty_panel.reason, empty_source.reason],
        'create': json_format.MessageToDict(create),
        'create_first': json_format.MessageToDict(create_first),
        'empty_create_has': {
            name: empty_create.HasField(name)
            for name in (
                'enabled',
                'asset_class',
                'exchange',
                'active',
                'tradable',
                'ramp_datasets_per_hour',
                'request_cap_per_hour',
                'corporate_action_window_pre_days',
                'corporate_action_window_post_days',
            )
        },
        'empty_create_plain': [empty_create.reason, empty_create.name, empty_create.source, empty_create.cadence],
        'round_trips': [round_trip(m) for m in every],
        'include_otc': {
            cls.__name__: include_otc(cls)
            for cls in (panels_pb2.PanelSettings, panels_pb2.SavePanelSettingsRequest, panels_pb2.CreatePanelRequest)
        },
        'old_size': {
            cls.__name__: old_size(cls)
            for cls in (panels_pb2.PanelSettings, panels_pb2.SavePanelSettingsRequest, panels_pb2.CreatePanelRequest)
        },
    },
    sys.stdout,
)
"""


@cache
def _probe() -> dict:
    """Import the panel messages from gen/ alone and render them as canonical JSON."""
    assert (GENERATED_ROOT / 'trader_joe' / 'proto' / 'ui' / 'v1' / 'panels_pb2.py').is_file(), (
        f'{GENERATED_ROOT} holds no ui/v1 panels_pb2: make proto did not generate it'
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


def test_the_panel_messages_generate_and_import_from_gen_alone():
    seen = _probe()
    assert all(Path(path).is_relative_to(GENERATED_ROOT) for path in seen['files_from']), seen['files_from']
    assert all(seen['round_trips']), 'a panel message does not parse back from its canonical JSON'


@pytest.mark.parametrize('message', ['PanelSettings', 'SavePanelSettingsRequest', 'CreatePanelRequest'])
def test_include_otc_absent_parses_false_and_a_true_is_on_the_wire(message: str):
    """ADR tj-ed4t8m D2 at the generated code: a body without includeOtc is off, a true is the key includeOtc.

    No HasField: implicit presence, so the false default is never on the wire and nothing can tell it from absent.

    Args:
        message: A message carrying the panel setting.
    """
    seen = _probe()['include_otc'][message]
    assert seen == {
        'absent': False,
        'sent_true': True,
        'true_json': {'includeOtc': True},
        'false_json': {},
        'has_field': 'refused',
    }


def test_a_panel_detail_renders_as_the_canonical_json_the_ui_reads():
    """int64 as a string, int32 as a number, enum NAMES, RFC 3339 'Z', lowerCamelCase keys with the unit kept."""
    seen = _probe()
    assert seen['detail'] == {
        'id': 'panel-1',
        'name': 'us',
        'source': 'DATA_SOURCE_ALPACA_API',
        'currentVersion': {
            'versionNo': 2,
            'settings': {
                'enabled': True,
                'cadence': 'PANEL_CADENCE_MONTHLY',
                'denySymbols': ['XYZ'],
                'tradable': False,
            },
            'savedAt': '2026-10-28T20:00:00Z',
            'actor': 'operator',
            'reason': 'owner asked',
            'effectivePass': {'passId': 'p-1', 'kind': 'PANEL_PASS_KIND_RELEASE', 'startedAt': '2026-10-28T20:15:00Z'},
        },
        'currentSourceVersion': {
            'source': 'DATA_SOURCE_ALPACA_API',
            'versionNo': 1,
            'settings': {
                'requestCapPerHour': 200,
                'corporateActionWindowPreDays': 90,
                'corporateActionWindowPostDays': 90,
            },
            'actor': 'operator',
            'reason': 'seed',
        },
        'counts': {'members': '1099511627776', 'collecting': '3', 'draining': '1', 'lagging': '2'},
    }


def test_a_version_no_pass_has_used_has_no_effective_pass_key():
    """Not yet effective is ABSENT, never an empty pass the UI would draw as effective at the epoch."""
    seen = _probe()
    assert 'effectivePass' not in seen['pending']
    assert seen['pending_has_effective'] is False


def test_a_member_a_history_day_a_marker_and_the_list_render_as_canonical_json():
    seen = _probe()
    assert seen['member'] == {
        'vendorAssetId': 'v-1',
        'symbol': 'AAPL',
        'status': 'PANEL_TRANSITION_DEFERRED',
        'cause': 'ramp_rate',
        'appearedOn': '2026-10-01',
        'health': 'DATASET_HEALTH_STATUS_LATE',
    }
    assert seen['day'] == {'day': '2026-10-27', 'state': 'ASSET_LIST_DAY_STATE_SAVED', 'changedCount': 4}
    assert seen['marker'] == {
        'kind': 'PANEL_MARKER_KIND_COLLECT_STOP',
        'actor': 'operator',
        'reason': 'halted',
        'datasetId': 'd-1',
        'label': 'AAPL 1d',
    }
    assert seen['listing'] == {'items': [{'id': 'panel-1', 'name': 'us'}]}


def test_an_unset_read_rate_has_no_key_and_a_zero_one_would_show():
    """ADR tj-2ayia2 D3: PanelSourceSettings 1-2 have explicit presence.

    The constructor given None (what the mapping passes for a NULL rate) leaves the field UNSET, so the canonical JSON
    has no key (pinned on the detail above); a 0, by contrast, is present and printed, so 0 can never stand in silently
    for 'no limit'.
    """
    seen = _probe()
    assert seen['source_rates_has'] == [False, True]
    assert 'rampDatasetsPerHour' not in seen['detail']['currentSourceVersion']['settings']
    assert seen['zero_read'] == {'rampDatasetsPerHour': 0, 'requestCapPerHour': 0}


def test_a_no_limit_source_write_sends_no_rate_key():
    """ADR tj-2ayia2: no limit is the rates absent from the body, the window day counts still present (0 included)."""
    seen = _probe()
    assert seen['no_limit'] == {
        'corporateActionWindowPreDays': 0,
        'corporateActionWindowPostDays': 0,
        'reason': 'no limit',
    }


def test_a_disable_and_a_zero_rate_source_write_their_false_and_zero_values():
    """Presence: enabled false and every rate 0 reach the route as values, so a 0 is never read as absent.

    Since ADR tj-2ayia2 a present rate 0 is REFUSED by the store (422, pinned in test_ui_panel_writes_route.py): the
    point here is only that the wire keeps it a value, so it cannot be mistaken for 'no limit'.
    """
    seen = _probe()
    assert seen['disable'] == {'enabled': False, 'cadence': 'PANEL_CADENCE_DAILY', 'reason': 'pause for the sitting'}
    assert seen['pause'] == {
        'rampDatasetsPerHour': 0,
        'requestCapPerHour': 0,
        'corporateActionWindowPreDays': 0,
        'corporateActionWindowPostDays': 0,
        'reason': 'pause',
    }


def test_an_empty_write_body_has_no_value_present_and_a_blank_reason():
    """'{}' parses to every presence field absent and reason '': the presence fields are refused, the reason is none.

    The reason is optional (owner ruling 2026-11-06), so '' here is the 'none given' value, not a refusal.
    """
    seen = _probe()
    assert seen['empty_has'] == dict.fromkeys(seen['empty_has'], False)
    assert len(seen['empty_has']) == 9
    assert seen['empty_reason'] == ['', '']


def test_a_create_body_without_source_settings_renders_as_the_canonical_json_the_ui_sends():
    """No cadence named (the MONTHLY default applies) and no source settings: neither key is sent.

    enabled false is a present value, as in the disable write; there is no size key (removed, 2026-11-06).
    """
    seen = _probe()
    assert seen['create'] == {'enabled': False, 'reason': 'new panel', 'name': 'us', 'source': 'DATA_SOURCE_ALPACA_API'}


def test_a_create_body_with_the_first_source_settings_renders_as_the_canonical_json_the_ui_sends():
    """The minimum ramp 1 is sent as a value; the two absent window day counts are absent keys (the store's 90)."""
    seen = _probe()
    assert seen['create_first'] == {
        'enabled': True,
        'cadence': 'PANEL_CADENCE_WEEKLY',
        'tradable': True,
        'reason': 'first panel',
        'name': 'us',
        'source': 'DATA_SOURCE_ALPACA_API',
        'rampDatasetsPerHour': 1,
        'requestCapPerHour': 200,
    }


def test_an_empty_create_body_has_no_value_present_and_blank_plain_fields():
    """'{}' parses to every presence field absent, reason and name '', source and cadence 0: refused, bar cadence."""
    seen = _probe()
    assert seen['empty_create_has'] == dict.fromkeys(seen['empty_create_has'], False)
    assert len(seen['empty_create_has']) == 9
    assert seen['empty_create_plain'] == ['', '', 0, 0]


@pytest.mark.parametrize('message', SIZED_ONCE)
def test_an_old_senders_field_3_never_parses_as_a_size(message: str):
    """Field 3 on the wire from a pre-ruling sender is an unknown field: kept byte for byte, never a value.

    The generated class has no size and no field 3; the bytes parse to no set field and render as '{}'; and a JSON body
    that still sends size is a parse error, not a silently dropped key.

    Args:
        message: A message that carried size before the owner ruling of 2026-11-06.
    """
    assert _probe()['old_size'][message] == {
        'declares_size': False,
        'number_3': False,
        'set_fields': [],
        'kept_unknown': True,
        'json': {},
        'json_size': 'refused',
    }
