"""`make proto`, run for real on scratch trees: the layout of decision tj-3mk3u5.42 F1 (tj-3mk3u5.44 gate 1-6).

F1 rules pinned here, each through the repository's own Makefile target with PROTO_SRC and PROTO_GEN
pointed at a temporary tree, so nothing in the checkout's proto/ or gen/ is read or written:

* rule 2, THE PLAIN ROOT: protoc runs with -I<proto root>, so one .proto imports another by its
  canonical path, the generated imports resolve as protoc writes them, and descriptor names are
  canonical ('trader_joe/proto/...') -- what every other consumer of proto/ records too;
* rule 2, NOTHING POST-PROCESSES: the output equals a bare grpc_tools.protoc run of the same inputs,
  byte for byte, apart from the package marker the target writes at trader_joe/proto/__init__.py;
* rule 3, CONFIGURATION ONLY: the output imports with its root on PYTHONPATH and nothing else, under
  `python -P`, so neither the working directory nor any code puts it on the path;
* rule 4, THE RESERVED ROOT: a .proto outside proto/trader_joe/proto/ fails the target, named, before
  anything is cleared or written;
* THE CLEARING: a removed .proto leaves no module and no directory behind, and nothing outside the
  generated package is touched;
* rule 5, PEP 420: trader_joe is a namespace -- no trader_joe/__init__.py anywhere in gen/ or in git --
  and it shares that namespace with another portion in one process.

NOTHING UNDER gen/ IS COMMITTED (user ruling 2026-10-05, reversing ADR tj-8konfu D3 and so the
staleness step this file used to defer to; see test_ci_invariants.py THE gRPC TOOLCHAIN). The target
is therefore what every consumer gets, which is why these gates run it for real, and it writes the
package marker protoc does not emit -- there is no longer a hand-committed file to keep. The static
image path model -- the Dockerfile's PYTHONPATH and COPY, the compose mounts, pytest.ini -- is
pinned in test_ci_invariants.py.
"""

import ast
import filecmp
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from common.tests.roots import REPO_ROOT


pytestmark = pytest.mark.build_infra

# THE TRUE REPOSITORY ROOT (tj-iontkq.2): gen/ and proto/ are root siblings of the service trees,
# the Makefile target this module invokes with `make -C` is at the root, and `git ls-files` must run
# in the checkout root to list the whole repository. REPO_ROOT, never SERVER_ROOT.
GENERATED_ROOT = REPO_ROOT / 'gen' / 'proto' / 'python'
# The package marker protoc does not emit: make proto writes it, and keeps it when it clears.
GUARD = Path('trader_joe') / 'proto' / '__init__.py'
MAKE_TIMEOUT_S = 180

# A two-file contract in the ruled hierarchy (F1 rule 8): a shared vocabulary, and a contract that
# imports it by its canonical path, plus a well-known type from google/protobuf.
SHARED_PROTO = Path('trader_joe/proto/shared/v1/shared.proto')
PROBE_PROTO = Path('trader_joe/proto/probe/v1/probe.proto')
PROTOS = {
    SHARED_PROTO: (
        'syntax = "proto3";\n\npackage trader_joe.proto.shared.v1;\n\nmessage Item {\n  string name = 1;\n}\n'
    ),
    PROBE_PROTO: (
        'syntax = "proto3";\n'
        '\n'
        'package trader_joe.proto.probe.v1;\n'
        '\n'
        'import "google/protobuf/timestamp.proto";\n'
        'import "trader_joe/proto/shared/v1/shared.proto";\n'
        '\n'
        'message Wrapper {\n'
        '  trader_joe.proto.shared.v1.Item item = 1;\n'
        '  google.protobuf.Timestamp at = 2;\n'
        '}\n'
        '\n'
        'service ProbeService {\n'
        '  rpc Get(trader_joe.proto.shared.v1.Item) returns (Wrapper);\n'
        '}\n'
    ),
}
SUFFIXES = ('_pb2.py', '_pb2.pyi', '_pb2_grpc.py')


def _outputs(proto: Path) -> set[Path]:
    """The three modules protoc writes for one .proto, relative to the output root."""
    return {proto.with_name(proto.stem + suffix) for suffix in SUFFIXES}


def _scratch(root: Path, protos: dict[Path, str]) -> tuple[Path, Path]:
    """A proto root and a generated root under ROOT, the generated package holding the real guard file."""
    src, gen = root / 'proto', root / 'gen' / 'proto' / 'python'
    for relative, text in protos.items():
        (src / relative).parent.mkdir(parents=True, exist_ok=True)
        (src / relative).write_text(text, encoding='utf-8')
    (gen / GUARD).parent.mkdir(parents=True)
    shutil.copyfile(GENERATED_ROOT / GUARD, gen / GUARD)
    return src, gen


def _make_proto(src: Path | str, gen: Path) -> subprocess.CompletedProcess:
    """Run the repository's own `make proto`, both roots overridden onto a scratch tree."""
    assert shutil.which('make'), 'make is not on PATH, so the target cannot be exercised'
    # Under `make test` these carry the outer make's flags, depth and command-line variables inward.
    env = {name: value for name, value in os.environ.items() if name not in ('MAKEFLAGS', 'MFLAGS', 'MAKELEVEL')}
    return subprocess.run(
        ['make', '--no-print-directory', '-C', str(REPO_ROOT), 'proto', f'PROTO_SRC={src}', f'PROTO_GEN={gen}'],
        capture_output=True,
        text=True,
        env=env,
        timeout=MAKE_TIMEOUT_S,
        check=False,
    )


def _ran(result: subprocess.CompletedProcess) -> str:
    return f'exit {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}'


def _files(root: Path) -> dict[Path, bytes]:
    """Every file under ROOT, relative, with its bytes."""
    return {path.relative_to(root): path.read_bytes() for path in sorted(root.rglob('*')) if path.is_file()}


def _bare_protoc(src: Path, out: Path) -> subprocess.CompletedProcess:
    """Run protoc as grpcio-tools ships it, with no Makefile around it: the definition of unprocessed output."""
    out.mkdir(parents=True)
    inputs = sorted(str(path) for path in src.rglob('*.proto'))
    return subprocess.run(
        [
            sys.executable,
            '-m',
            'grpc_tools.protoc',
            f'-I{src}',
            f'--python_out={out}',
            f'--grpc_python_out={out}',
            f'--pyi_out={out}',
            *inputs,
        ],
        capture_output=True,
        text=True,
        timeout=MAKE_TIMEOUT_S,
        check=False,
    )


@pytest.fixture(scope='module')
def generated(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """The two-file contract, generated once by the real target: (proto root, generated root)."""
    src, gen = _scratch(tmp_path_factory.mktemp('two-file'), PROTOS)
    result = _make_proto(src, gen)
    assert result.returncode == 0, _ran(result)
    return src, gen


# ---------------------------------------------------------------------------------------------------
# GATE 1 AND 3: THE PLAIN ROOT


def test_a_two_file_contract_generates_into_the_proto_package_tree(generated: tuple[Path, Path]):
    """The output tree mirrors the proto packages, and nothing lands anywhere else."""
    _, gen = generated
    expected = {GUARD} | _outputs(PROBE_PROTO) | _outputs(SHARED_PROTO)
    assert set(_files(gen)) == expected, sorted(map(str, _files(gen)))


def test_generated_imports_are_protocs_canonical_ones(generated: tuple[Path, Path]):
    """Rule 2: first-party imports name trader_joe.proto as protoc writes them; google.protobuf is untouched."""
    _, gen = generated
    probe = (gen / PROBE_PROTO.with_name('probe_pb2.py')).read_text(encoding='utf-8').splitlines()
    assert 'from google.protobuf import timestamp_pb2 as google_dot_protobuf_dot_timestamp__pb2' in probe
    assert any(line.startswith('from trader_joe.proto.shared.v1 import shared_pb2 as ') for line in probe), probe
    stub = (gen / PROBE_PROTO.with_name('probe_pb2_grpc.py')).read_text(encoding='utf-8').splitlines()
    assert any(line.startswith('from trader_joe.proto.probe.v1 import probe_pb2 as ') for line in stub), stub
    for relative in _files(gen):
        text = (gen / relative).read_text(encoding='utf-8')
        assert 'common.rpc' not in text and 'gen.proto' not in text, f'{relative} names a server-side prefix'


_IMPORT_PROBE = """
import json, sys

import trader_joe
from google.protobuf import timestamp_pb2
from trader_joe.proto.probe.v1 import probe_pb2, probe_pb2_grpc
from trader_joe.proto.shared.v1 import shared_pb2

wrapper = probe_pb2.Wrapper(item=shared_pb2.Item(name='x'), at=timestamp_pb2.Timestamp(seconds=1))
json.dump(
    {
        'namespace_origin': trader_joe.__spec__.origin,
        'namespace_path': list(trader_joe.__path__),
        'probe_file': probe_pb2.__file__,
        'descriptor': probe_pb2.DESCRIPTOR.name,
        'shared_descriptor': shared_pb2.DESCRIPTOR.name,
        'dependencies': sorted(dependency.name for dependency in probe_pb2.DESCRIPTOR.dependencies),
        'service': probe_pb2.DESCRIPTOR.services_by_name['ProbeService'].full_name,
        'round_trip': probe_pb2.Wrapper.FromString(wrapper.SerializeToString()) == wrapper,
        'stub': hasattr(probe_pb2_grpc, 'ProbeServiceStub'),
        'sys_path': sys.path,
    },
    sys.stdout,
)
"""


def test_the_contract_imports_with_its_root_on_pythonpath_and_nothing_else(
    generated: tuple[Path, Path], tmp_path: Path
):
    """Rules 2 and 3: the generated root on PYTHONPATH is all it takes; -P keeps the working directory off the path."""
    _, gen = generated
    # No bytecode: the module fixture's tree is compared file by file after this.
    env = {**os.environ, 'PYTHONPATH': str(gen), 'PYTHONDONTWRITEBYTECODE': '1'}
    result = subprocess.run(
        [sys.executable, '-P', '-c', _IMPORT_PROBE],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, _ran(result)
    seen = json.loads(result.stdout)
    assert seen['descriptor'] == str(PROBE_PROTO), 'the descriptor name is not the canonical path'
    assert seen['shared_descriptor'] == str(SHARED_PROTO)
    assert seen['dependencies'] == ['google/protobuf/timestamp.proto', str(SHARED_PROTO)]
    assert seen['service'] == 'trader_joe.proto.probe.v1.ProbeService'
    assert seen['round_trip'] and seen['stub']
    assert Path(seen['probe_file']).is_relative_to(gen), seen['probe_file']
    assert seen['namespace_origin'] is None, 'trader_joe imported as a regular package, not a namespace'
    assert seen['namespace_path'] == [str(gen / 'trader_joe')]
    assert '' not in seen['sys_path'] and str(tmp_path) not in seen['sys_path'], seen['sys_path']
    assert not any(Path(entry).is_relative_to(GENERATED_ROOT) for entry in seen['sys_path'] if entry), (
        "the checkout's own generated tree is on the path, so this proves nothing about the scratch one"
    )


# ---------------------------------------------------------------------------------------------------
# GATE 2: NOTHING POST-PROCESSES


def test_make_proto_writes_exactly_what_bare_protoc_writes(generated: tuple[Path, Path], tmp_path: Path):
    """Rule 2: byte for byte, every file, apart from the hand-committed guard; and protoc writes no __init__.py."""
    src, gen = generated
    bare = tmp_path / 'bare'
    result = _bare_protoc(src, bare)
    assert result.returncode == 0, _ran(result)
    made, reference = _files(gen), _files(bare)
    assert not any(path.name == '__init__.py' for path in reference), 'protoc now writes __init__.py files'
    assert set(made) - set(reference) == {GUARD}, sorted(map(str, set(made) ^ set(reference)))
    assert set(reference) <= set(made), sorted(map(str, set(reference) - set(made)))
    differ = sorted(str(path) for path in reference if made[path] != reference[path])
    assert not differ, f'make proto changed protoc output in {differ}: something post-processes it'
    match, mismatch, errors = filecmp.cmpfiles(gen, bare, [str(path) for path in reference], shallow=False)
    assert not mismatch and not errors and len(match) == len(reference)


# ---------------------------------------------------------------------------------------------------
# GATE 4: THE CLEARING


def test_a_removed_proto_leaves_no_module_and_no_directory_behind(tmp_path: Path):
    """What make proto clears equals what it writes: the package beneath the guard, and nothing outside it.

    A stale module is the failure: it imports, it is committed, and CI's staleness step never sees
    it, because regeneration leaves a tracked, unchanged file exactly as it was. An empty directory
    left behind would still import as a namespace package. A hand-dropped file in the package goes
    too. The guard is kept byte for byte, and a file beside the package under PROTO_GEN -- where PR 3
    adds packaging metadata (F1 rule 7) -- is never touched.
    """
    src, gen = _scratch(tmp_path, PROTOS)
    first = _make_proto(src, gen)
    assert first.returncode == 0, _ran(first)
    assert _outputs(PROBE_PROTO) <= set(_files(gen))

    (src / PROBE_PROTO).unlink()
    stray = gen / 'trader_joe' / 'proto' / 'stray' / 'notes.txt'
    stray.parent.mkdir(parents=True)
    stray.write_text('hand-dropped\n', encoding='utf-8')
    beside = gen / 'pyproject.toml'
    beside.write_text('[project]\nname = "trader-joe-proto"\n', encoding='utf-8')
    guard = (gen / GUARD).read_bytes()

    second = _make_proto(src, gen)
    assert second.returncode == 0, _ran(second)
    assert set(_files(gen)) == {GUARD, Path('pyproject.toml')} | _outputs(SHARED_PROTO), sorted(map(str, _files(gen)))
    assert not (gen / PROBE_PROTO.parent.parent).exists(), 'the removed contract left its directory behind'
    assert not stray.parent.exists()
    assert (gen / GUARD).read_bytes() == guard
    assert beside.read_text(encoding='utf-8') == '[project]\nname = "trader-joe-proto"\n'


# ---------------------------------------------------------------------------------------------------
# GATE 5: THE RESERVED ROOT, AND THE GUARD FILE


def test_a_proto_outside_the_reserved_root_fails_named_and_changes_nothing(tmp_path: Path):
    """Rule 4: every .proto lies under proto/trader_joe/proto/; anything else fails before a byte is cleared.

    Three shapes: the proto root itself, a sibling package of trader_joe/proto, and a near miss whose
    directory only starts with 'proto'. Each is named. The generated tree is left byte for byte as it
    was, and nothing is generated for any input, the valid one included.
    """
    src, gen = _scratch(tmp_path, {SHARED_PROTO: PROTOS[SHARED_PROTO]})
    first = _make_proto(src, gen)
    assert first.returncode == 0, _ran(first)
    before = _files(gen)

    outside = [Path('rogue.proto'), Path('trader_joe/stray/v1/stray.proto'), Path('trader_joe/protos/v1/near.proto')]
    for relative in outside:
        (src / relative).parent.mkdir(parents=True, exist_ok=True)
        (src / relative).write_text('syntax = "proto3";\npackage rogue.v1;\nmessage R {}\n', encoding='utf-8')
    (src / PROBE_PROTO).parent.mkdir(parents=True)
    (src / PROBE_PROTO).write_text(PROTOS[PROBE_PROTO], encoding='utf-8')

    result = _make_proto(src, gen)
    assert result.returncode != 0, _ran(result)
    assert 'trader_joe/proto/' in result.stderr, result.stderr
    unnamed = [str(relative) for relative in outside if str(src / relative) not in result.stderr]
    assert not unnamed, f'make proto refused without naming {unnamed}:\n{result.stderr}'
    assert str(src / SHARED_PROTO) not in result.stderr, 'a file inside the reserved root was named as outside it'
    assert _files(gen) == before, 'the refused run changed the generated tree'
    assert not list(tmp_path.rglob('*rogue_pb2*')) and not list(tmp_path.rglob('*probe_pb2*'))


def test_a_trailing_slash_on_the_proto_root_is_not_outside_it(tmp_path: Path):
    """The guard compares paths, not spellings: PROTO_SRC=<dir>/ is the same root (builder's 18:52 fix)."""
    src, gen = _scratch(tmp_path, PROTOS)
    result = _make_proto(f'{src}/', gen)
    assert result.returncode == 0, _ran(result)
    assert set(_files(gen)) == {GUARD} | _outputs(PROBE_PROTO) | _outputs(SHARED_PROTO)


def test_without_its_guard_file_an_existing_package_is_not_cleared(tmp_path: Path):
    """The guard that keeps the target from clearing a directory that is not the generated package.

    It applies to a package that ALREADY EXISTS, which is the only case that can delete anything.
    A generated root that does not exist yet is created instead -- the test below.
    """
    src, gen = _scratch(tmp_path, PROTOS)
    first = _make_proto(src, gen)
    assert first.returncode == 0, _ran(first)
    (gen / GUARD).unlink()
    before = _files(gen)

    result = _make_proto(src, gen)
    assert result.returncode != 0, _ran(result)
    assert str(GUARD) in result.stderr, result.stderr
    assert _files(gen) == before, 'the refused run changed the generated tree'


def test_a_generated_root_that_does_not_exist_yet_is_created_with_its_marker(tmp_path: Path):
    """The fresh-clone case, which is now every clone: nothing under gen/ is committed.

    protoc emits no trader_joe/proto/__init__.py and without it trader_joe.proto is not an importable
    package, so the target writes it. Before the 2026-10-05 ruling that one file was hand-committed
    and the target refused when it was absent; a checkout that refused to generate because the thing
    generation produces was missing would be a bootstrap with no entry point.
    """
    src = tmp_path / 'proto'
    for relative, text in PROTOS.items():
        (src / relative).parent.mkdir(parents=True, exist_ok=True)
        (src / relative).write_text(text, encoding='utf-8')
    gen = tmp_path / 'gen' / 'proto' / 'python'
    assert not gen.exists(), 'the point of this test is that nothing under the generated root exists yet'

    result = _make_proto(src, gen)
    assert result.returncode == 0, _ran(result)
    assert set(_files(gen)) == {GUARD} | _outputs(PROBE_PROTO) | _outputs(SHARED_PROTO)
    assert ast.parse((gen / GUARD).read_text(encoding='utf-8')).body == [], 'the marker must be a comment only'


# ---------------------------------------------------------------------------------------------------
# GATE 6: PEP 420 (F1 rule 5)


def test_no_trader_joe_init_exists_and_the_marker_is_the_only_init_under_gen():
    """No portion may ship trader_joe/__init__.py; trader_joe/proto/__init__.py is the one __init__ in gen/.

    The DISK is the authority now that nothing under gen/ is committed (user ruling 2026-10-05):
    what this run imports, and what `make proto` just wrote, are the same tree. git is still read,
    for the half that is about the index -- no portion may commit a namespace __init__.py, and
    nothing under gen/ may be committed at all, which is the ruling itself.
    """
    tracked = subprocess.run(
        ['git', 'ls-files'], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    namespace_inits = [path for path in tracked if Path(path).parts[-2:] == ('trader_joe', '__init__.py')]
    assert not namespace_inits, (
        f'a trader_joe/__init__.py is tracked, which breaks every other portion: {namespace_inits}'
    )
    gen_root = GENERATED_ROOT.relative_to(REPO_ROOT).parent.parent
    tracked_under_gen = sorted(path for path in tracked if Path(path).is_relative_to(gen_root))
    assert not tracked_under_gen, f'{gen_root}/ is generated and must not be committed; git tracks {tracked_under_gen}'
    on_disk = {path.relative_to(REPO_ROOT) for path in (REPO_ROOT / gen_root).rglob('__init__.py')}
    assert on_disk == {GENERATED_ROOT.relative_to(REPO_ROOT) / GUARD}, sorted(map(str, on_disk))


def test_the_guard_file_does_nothing_on_import():
    """trader_joe.proto/__init__.py runs on every generated import; it is a comment, never code."""
    guard = GENERATED_ROOT / GUARD
    assert guard.is_file()
    assert ast.parse(guard.read_text(encoding='utf-8')).body == [], f'{guard} has statements'
    assert guard.read_text(encoding='utf-8').strip(), f'{guard} is empty; it should say where it comes from'


_NAMESPACE_PROBE = """
import json, sys

import trader_joe
import trader_joe.common
from trader_joe.proto.ping.v1 import ping_pb2

json.dump(
    {
        'origin': trader_joe.__spec__.origin,
        'path': sorted(trader_joe.__path__),
        'common': trader_joe.common.PORTION,
        'ping': ping_pb2.DESCRIPTOR.name,
    },
    sys.stdout,
)
"""


def test_trader_joe_is_one_namespace_the_generated_tree_shares_with_another_portion(tmp_path: Path):
    """Rule 5's reason: the committed tree and a hand-written portion (trader_joe.common, tj-yw8cok) import together.

    With a trader_joe/__init__.py in either, trader_joe stops being a namespace and the other portion
    is no longer importable.
    """
    portion = tmp_path / 'portion'
    (portion / 'trader_joe' / 'common').mkdir(parents=True)
    (portion / 'trader_joe' / 'common' / '__init__.py').write_text("PORTION = 'common'\n", encoding='utf-8')
    env = {
        **os.environ,
        'PYTHONPATH': os.pathsep.join([str(GENERATED_ROOT), str(portion)]),
        'PYTHONDONTWRITEBYTECODE': '1',
    }
    result = subprocess.run(
        [sys.executable, '-P', '-c', _NAMESPACE_PROBE],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, _ran(result)
    seen = json.loads(result.stdout)
    assert seen['origin'] is None, 'trader_joe is a regular package: a trader_joe/__init__.py exists'
    assert seen['path'] == sorted([str(GENERATED_ROOT / 'trader_joe'), str(portion / 'trader_joe')])
    assert seen['common'] == 'common'
    assert seen['ping'] == 'trader_joe/proto/ping/v1/ping.proto'
