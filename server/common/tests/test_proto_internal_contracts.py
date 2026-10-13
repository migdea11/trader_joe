"""The internal contracts tj-grna9p.44 adds or widens: store.proto's Bootstrap, and quota_key everywhere.

The design is tj-grna9p.44's architect re-baseline of 2026-10-11 (items 1, 5 and the acceptance):

* BOOTSTRAP RUNS ingest -> store, the opposite direction, so it is NOT on IngestService: a new file
  trader_joe/proto/internal/store/v1/store.proto declares StoreService with one unary rpc
  Bootstrap(BootstrapRequest) returns (BootstrapResponse) -- buf STANDARD names, not *Reply.
* A CONTRACT IMPORTS market/v1 AND NEVER ANOTHER CONTRACT, so store.proto names a request by a
  RequestRef and does not embed FetchDatasetRequest.
* quota_key IS A NON-SECRET CONFIGURED LABEL, NEVER KEY MATERIAL AND NEVER A HASH OF IT, and every
  comment on a quota_key field says so; no field on either internal contract names credential material.

Read through protoc's own descriptors (``common/tests/proto_descriptors``) and, for comments, protoc's
source info -- never by importing the generated package, which TID251 keeps inside common/rpc.
"""

import re
import subprocess
import sys
import tempfile
from functools import cache
from pathlib import Path

import pytest
from google.protobuf import descriptor_pb2

from common.tests.proto_descriptors import PROTO_ROOT, PROTOC_TIMEOUT_S, file_named, messages


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


def test_bootstrap_is_one_unary_rpc_on_its_own_store_service():
    """Item 5: data_store serves it, data_ingest calls it, so it lives in its own package and service."""
    store = file_named(STORE)

    assert store.package == 'trader_joe.proto.internal.store.v1'
    assert [service.name for service in store.service] == ['StoreService']
    (bootstrap,) = store.service[0].method
    assert bootstrap.name == 'Bootstrap'
    assert (bootstrap.client_streaming, bootstrap.server_streaming) == (False, False)
    assert bootstrap.input_type == '.trader_joe.proto.internal.store.v1.BootstrapRequest'
    assert bootstrap.output_type == '.trader_joe.proto.internal.store.v1.BootstrapResponse'

    ingest_methods = [method.name for method in file_named(INGEST).service[0].method]
    assert 'Bootstrap' not in ingest_methods, 'Bootstrap runs ingest -> store and does not belong on IngestService'


def test_store_proto_imports_no_other_contract():
    """'A contract may import market/v1 and never another contract': store.proto embeds no fetch request."""
    assert all(not dependency.startswith('trader_joe/proto/internal/') for dependency in file_named(STORE).dependency)


STORE_MESSAGES = {
    'QuotaWindow': [('quota_key', 1, 'TYPE_STRING', False), ('window_seconds', 2, 'TYPE_UINT64', False)],
    'BootstrapRequest': [('windows', 1, '.trader_joe.proto.internal.store.v1.QuotaWindow', True)],
    'RequestRef': [
        ('request_id', 1, 'TYPE_STRING', False),
        ('quota_key', 2, 'TYPE_STRING', False),
        ('estimated_vendor_calls', 3, 'TYPE_UINT64', False),
    ],
    'WindowUsage': [
        ('quota_key', 1, 'TYPE_STRING', False),
        ('window_seconds', 2, 'TYPE_UINT64', False),
        ('calls_in_window', 3, 'TYPE_UINT64', False),
    ],
    'BootstrapResponse': [
        ('in_flight', 1, '.trader_joe.proto.internal.store.v1.RequestRef', True),
        ('pending', 2, '.trader_joe.proto.internal.store.v1.RequestRef', True),
        ('usage', 3, '.trader_joe.proto.internal.store.v1.WindowUsage', True),
    ],
}


def test_store_proto_declares_exactly_the_ruled_messages():
    """Item 5's shapes, and no others: the store resets in-flight rows itself, ingest only counts quota."""
    declared = messages(file_named(STORE))

    assert set(declared) == set(STORE_MESSAGES)
    for name, fields in STORE_MESSAGES.items():
        assert [_shape(field) for field in declared[name].field] == fields, name


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
