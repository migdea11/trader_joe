"""The repository's .proto files as DESCRIPTORS, compiled on demand and imported by nothing generated.

WHY NOT JUST IMPORT ``trader_joe.proto``. TID251 bans the generated package everywhere but
``common/rpc`` (pyproject.toml; ADR tj-8konfu D3), and a test is not the seam. Reading a descriptor set
that protoc writes keeps the ban intact while still asking the REAL compiler what the contract says --
the alternative, parsing .proto text with regexes, would re-implement protoc badly and would answer
questions about the text rather than about the schema.

The descriptor set is built from ``proto/`` with the canonical include root (decision tj-3mk3u5.42 F1
rule 2), so file names here are the canonical ones -- ``trader_joe/proto/<domain>/v1/<file>.proto`` --
which is exactly what every other consumer of ``proto/`` records.

This module is a HELPER, not a test file: it declares no test and is imported the way
``common/tests/image_path.py`` is.
"""

import subprocess
import sys
import tempfile
from functools import cache
from pathlib import Path

from google.protobuf import descriptor_pb2

from common.tests.roots import REPO_ROOT


# THE TRUE REPOSITORY ROOT (tj-iontkq.2): proto/ is a root sibling of the service trees and is not
# carried down with them, so this is REPO_ROOT and never SERVER_ROOT.
PROTO_ROOT = REPO_ROOT / 'proto'
# The one reserved root every .proto lies under (F1 rule 4), as a canonical path prefix.
FIRST_PARTY_PREFIX = 'trader_joe/proto/'
PROTOC_TIMEOUT_S = 180


@cache
def descriptor_set() -> descriptor_pb2.FileDescriptorSet:
    """Compile every .proto under ``proto/`` and return the resulting FileDescriptorSet.

    ``--include_imports`` pulls in ``google/protobuf/*`` too, so a caller can resolve a well-known
    type by name instead of assuming it. Cached because protoc costs a subprocess and the sources do
    not change inside a run.

    Returns:
        descriptor_pb2.FileDescriptorSet: Every compiled file, first-party and imported alike.

    Raises:
        AssertionError: If protoc fails, which means ``proto/`` does not compile at all.
    """
    inputs = sorted(str(path.relative_to(PROTO_ROOT)) for path in PROTO_ROOT.rglob('*.proto'))
    assert inputs, f'no .proto files under {PROTO_ROOT}'
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / 'descriptor_set.bin'
        result = subprocess.run(
            [
                sys.executable,
                '-m',
                'grpc_tools.protoc',
                f'-I{PROTO_ROOT}',
                f'--descriptor_set_out={out}',
                '--include_imports',
                *inputs,
            ],
            capture_output=True,
            text=True,
            timeout=PROTOC_TIMEOUT_S,
            check=False,
        )
        assert result.returncode == 0, f'protoc failed on proto/:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}'
        parsed = descriptor_pb2.FileDescriptorSet()
        parsed.ParseFromString(out.read_bytes())
    return parsed


@cache
def first_party_files() -> dict[str, descriptor_pb2.FileDescriptorProto]:
    """This repository's own .proto files, keyed by canonical file name.

    Returns:
        dict[str, descriptor_pb2.FileDescriptorProto]: Files under ``trader_joe/proto/``, excluding
        the well-known types ``--include_imports`` dragged in.
    """
    return {file.name: file for file in descriptor_set().file if file.name.startswith(FIRST_PARTY_PREFIX)}


def file_named(name: str) -> descriptor_pb2.FileDescriptorProto:
    """One first-party file by its canonical name.

    Args:
        name: The canonical path, e.g. ``trader_joe/proto/market/v1/bar.proto``.

    Returns:
        descriptor_pb2.FileDescriptorProto: That file's descriptor.

    Raises:
        AssertionError: If no such file was compiled, which a rename or a move would cause.
    """
    files = first_party_files()
    assert name in files, f'{name} is not among the compiled first-party files: {sorted(files)}'
    return files[name]


def messages(file: descriptor_pb2.FileDescriptorProto) -> dict[str, descriptor_pb2.DescriptorProto]:
    """Top-level messages of one file, keyed by name.

    Args:
        file: A compiled file descriptor.

    Returns:
        dict[str, descriptor_pb2.DescriptorProto]: Its top-level messages. Nested map entries, which
        protoc synthesises for a ``map<k, v>`` field, are not top-level and so never appear.
    """
    return {message.name: message for message in file.message_type}


def field_names(message: descriptor_pb2.DescriptorProto) -> list[str]:
    """Field names of a message, in declaration order.

    Args:
        message: A message descriptor.

    Returns:
        list[str]: The field names.
    """
    return [field.name for field in message.field]


def real_oneofs(message: descriptor_pb2.DescriptorProto) -> dict[str, list[str]]:
    """The message's DECLARED oneofs and their arm names, excluding protoc's synthetic ones.

    proto3's ``optional`` keyword is implemented as a one-arm synthetic oneof, so ``Bar``'s optional
    ``trade_count`` shows up as a oneof named ``_trade_count``. Counting those as declared oneofs would
    make "FetchAck has exactly one oneof" meaningless the moment a field became optional.

    Args:
        message: A message descriptor.

    Returns:
        dict[str, list[str]]: Declared oneof name to its arms' field names, in declaration order.
    """
    synthetic = {field.oneof_index for field in message.field if field.proto3_optional}
    return {
        oneof.name: [
            field.name for field in message.field if field.HasField('oneof_index') and field.oneof_index == index
        ]
        for index, oneof in enumerate(message.oneof_decl)
        if index not in synthetic
    }
