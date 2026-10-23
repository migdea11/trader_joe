"""The content stamp for generated protobuf code: tools/proto_digest.sh and the make targets that write it.

Bead tj-grna9p.164 (it supersedes the mtime guard of tj-iee6sn.29). A generated-code guard asks "was
gen/proto/<language> generated from THESE protos", by content. The one algorithm is
tools/proto_digest.sh <python|ts>: one sha256 over a manifest of a format-version line, a target line
and one `f <sha256 of content> <relative path>` line per input, the paths sorted bytewise. The inputs
are proto/**/*.proto, plus the repo-root buf.yaml and web/buf.gen.yaml for ts (format v2, bead
tj-grna9p.185: buf generate reads buf.yaml to resolve the module; protoc reads neither file, so the
python inputs are unchanged and only the version line moves its digest). For ts both files must be
present, readable regular files, never links. `make proto` writes gen/proto/python.digest as its LAST
step, the old stamp deleted FIRST, so a failed generation leaves no stamp. gen/proto/ts.digest has ONE
writer, web/scripts/gen-proto.sh, which every `npm run gen:proto` runs (tj-grna9p.186,
test_web_gen_proto_stamp); `make gen-proto-ts` deletes a stale stamp and calls it, and never digests or
writes the stamp itself (tj-grna9p.188). Both stamps sit outside the trees their generator clears.

The script runs for real, from a copy in a scratch repository (it resolves the repository from its own
location). The make targets run the repository's real recipes from a scratch tree, with protoc (through
`uv`), npm and buf stood in; the npm stand-in runs the real gen-proto.sh as npm would, and the digest
script is wrapped to count its calls. Each stand-in records whether a stamp existed at the moment it
ran, which is what proves "delete first, write last".

The manifest is the documented contract the agent MCP's trusted copy of the script must agree with, so
_expected() spells it from the specification and the script's output is compared against it: a script
that ignored content, order or an input would agree with itself and not with this.

NOT RUN here: real protoc, real buf or npm (test_web_gen_proto_stamp runs real npm), and the MCP reader
(tj-iee6sn.33).
"""

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from common.tests.test_ci_invariants import MAKEFILE, REPO_ROOT, _make_recipe, _run_make, _subprocess_env
from common.tests.test_make_lint import Stubs, _executable


pytestmark = pytest.mark.build_infra

SCRIPT = REPO_ROOT / 'tools' / 'proto_digest.sh'
BUF_GEN = REPO_ROOT / 'web' / 'buf.gen.yaml'
PACKAGE_JSON = REPO_ROOT / 'web' / 'package.json'
GEN_SCRIPT = REPO_ROOT / 'web' / 'scripts' / 'gen-proto.sh'
MANIFEST_VERSION = b'trader_joe proto-digest v2'
DIGEST_LINE = re.compile(r'[0-9a-f]{64}\n')
PYTHON_STAMP = Path('gen/proto/python.digest')
TS_STAMP = Path('gen/proto/ts.digest')
TIMEOUT_S = 60

A_PROTO = 'trader_joe/proto/a/v1/a.proto'
B_PROTO = 'trader_joe/proto/b/v1/b.proto'
BASE_FILES = {
    A_PROTO: b'syntax = "proto3";\npackage trader_joe.proto.a.v1;\nmessage A { string x = 1; }\n',
    B_PROTO: b'syntax = "proto3";\npackage trader_joe.proto.b.v1;\nmessage B { int64 y = 1; }\n',
}
BUF_GEN_TEXT = b'version: v2\nclean: true\nplugins:\n  - local: protoc-gen-es\n    out: ../gen/proto/ts\n'
BUF_YAML_TEXT = b'version: v2\nmodules:\n  - path: proto\n'


# ---------------------------------------------------------------------------------------------------
# HELPERS


def _tree(
    tmp_path: Path,
    files: dict[str, bytes] | None = None,
    buf_gen: bytes | None = BUF_GEN_TEXT,
    buf_yaml: bytes | None = BUF_YAML_TEXT,
) -> Path:
    """A scratch repository: a copy of the real script, proto/ holding `files`, buf.yaml and web/buf.gen.yaml."""
    root = tmp_path / 'repo'
    (root / 'tools').mkdir(parents=True)
    shutil.copy2(SCRIPT, root / 'tools' / 'proto_digest.sh')
    (root / 'proto').mkdir()
    for relative, content in (BASE_FILES if files is None else files).items():
        path = root / 'proto' / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    if buf_gen is not None:
        (root / 'web').mkdir()
        (root / 'web' / 'buf.gen.yaml').write_bytes(buf_gen)
    if buf_yaml is not None:
        (root / 'buf.yaml').write_bytes(buf_yaml)
    return root


def _run(root: Path, *arguments: str, cwd: Path | None = None, env: dict[str, str] | None = None):
    """Run root's tools/proto_digest.sh from `cwd` (by default a directory outside the repository)."""
    outside = root.parent / 'elsewhere'
    outside.mkdir(exist_ok=True)
    return subprocess.run(
        [str(root / 'tools' / 'proto_digest.sh'), *arguments],
        cwd=cwd or outside,
        capture_output=True,
        text=True,
        env=env or _subprocess_env(),
        timeout=TIMEOUT_S,
        check=False,
    )


def _digest(root: Path, target: str, **kwargs) -> str:
    """The digest the script prints, holding it to the output contract: one hex line, nothing on stderr."""
    result = _run(root, target, **kwargs)
    assert result.returncode == 0, f'exit {result.returncode}: {result.stderr}'
    assert DIGEST_LINE.fullmatch(result.stdout), f'not exactly one digest line: {result.stdout!r}'
    assert result.stderr == '', f'a successful run wrote to stderr: {result.stderr!r}'
    return result.stdout.strip()


def _inputs(root: Path, target: str) -> list[bytes]:
    """The specified inputs: every regular *.proto under proto/ (no link followed); for ts also buf.yaml and web/buf.gen.yaml."""
    found = []
    for directory, _, names in os.walk(root / 'proto'):
        for name in names:
            path = Path(directory) / name
            if name.endswith('.proto') and path.is_file() and not path.is_symlink():
                found.append(os.fsencode(path.relative_to(root)))
    if target == 'ts':
        found += [b'buf.yaml', b'web/buf.gen.yaml']
    return sorted(found)


def _expected(root: Path, target: str, version: bytes = MANIFEST_VERSION) -> str:
    """The digest of the documented manifest, spelled from the specification and not from the script."""
    manifest = version + b'\n' + b'target ' + target.encode() + b'\n'
    for relative in _inputs(root, target):
        content = (root / os.fsdecode(relative)).read_bytes()
        manifest += b'f ' + hashlib.sha256(content).hexdigest().encode() + b' ' + relative + b'\n'
    return hashlib.sha256(manifest).hexdigest()


def _refused(result: subprocess.CompletedProcess) -> None:
    assert result.returncode != 0, f'not refused; printed {result.stdout!r}'
    assert result.stdout == '', f'a refusal printed something on stdout: {result.stdout!r}'
    assert result.stderr.strip(), 'a refusal gave no reason on stderr'


# ---------------------------------------------------------------------------------------------------
# THE SCRIPT: the digest


def test_the_script_is_committed_executable():
    """Mode 100755 in the index, so a fresh clone can run it as `tools/proto_digest.sh` (the recipes do)."""
    staged = subprocess.run(
        ['git', 'ls-files', '-s', '--', 'tools/proto_digest.sh'],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert staged.split()[:1] == ['100755'], f'tools/proto_digest.sh is not committed executable: {staged!r}'
    assert os.access(SCRIPT, os.X_OK)


@pytest.mark.parametrize('target', ['python', 'ts'])
def test_the_real_repository_digest_is_the_documented_manifest(target: str):
    """The real script over the real proto/ and web/buf.gen.yaml prints the specified digest."""
    assert _digest(REPO_ROOT, target) == _expected(REPO_ROOT, target)


@pytest.mark.parametrize('target', ['python', 'ts'])
def test_the_same_inputs_give_the_same_digest(tmp_path: Path, target: str):
    root = _tree(tmp_path)
    first = _digest(root, target)
    assert _digest(root, target) == first
    assert first == _expected(root, target)


def test_the_digest_does_not_depend_on_the_working_directory(tmp_path: Path):
    root = _tree(tmp_path)
    expected = _digest(root, 'python')
    assert _digest(root, 'python', cwd=root) == expected
    assert _digest(root, 'python', cwd=root / 'proto' / 'trader_joe') == expected


def test_python_and_ts_digests_differ(tmp_path: Path):
    root = _tree(tmp_path)
    assert _digest(root, 'python') != _digest(root, 'ts')


@pytest.mark.parametrize('target', ['python', 'ts'])
def test_one_changed_byte_in_one_proto_changes_the_digest(tmp_path: Path, target: str):
    root = _tree(tmp_path)
    before = _digest(root, target)
    path = root / 'proto' / B_PROTO
    path.write_bytes(path.read_bytes().replace(b'y = 1', b'y = 2'))
    after = _digest(root, target)
    assert after != before
    assert after == _expected(root, target)


def test_a_trailing_newline_changes_the_digest(tmp_path: Path):
    root = _tree(tmp_path)
    path = root / 'proto' / A_PROTO
    path.write_bytes(path.read_bytes().rstrip(b'\n'))
    without = _digest(root, 'python')
    path.write_bytes(path.read_bytes() + b'\n')
    assert _digest(root, 'python') != without


@pytest.mark.parametrize('target', ['python', 'ts'])
def test_adding_removing_or_renaming_a_proto_changes_the_digest(tmp_path: Path, target: str):
    root = _tree(tmp_path)
    seen = {_digest(root, target)}
    extra = root / 'proto' / 'trader_joe/proto/c/v1/c.proto'
    extra.parent.mkdir(parents=True)
    extra.write_bytes(b'syntax = "proto3";\n')
    seen.add(_digest(root, target))
    extra.rename(extra.with_name('d.proto'))  # same content, another name
    seen.add(_digest(root, target))
    (root / 'proto' / B_PROTO).unlink()
    seen.add(_digest(root, target))
    assert len(seen) == 4, 'an add, a rename or a remove left the digest where it was'
    assert seen.issuperset({_expected(root, target)})


def test_swapping_two_files_contents_changes_the_digest(tmp_path: Path):
    """Each hash is bound to its path: the same multiset of contents under other names is another input."""
    swapped = {A_PROTO: BASE_FILES[B_PROTO], B_PROTO: BASE_FILES[A_PROTO]}
    assert _digest(_tree(tmp_path / 'x'), 'python') != _digest(_tree(tmp_path / 'y', swapped), 'python')


def test_buf_gen_yaml_moves_the_ts_digest_and_not_the_python_one(tmp_path: Path):
    root = _tree(tmp_path)
    python, ts = _digest(root, 'python'), _digest(root, 'ts')
    (root / 'web' / 'buf.gen.yaml').write_bytes(BUF_GEN_TEXT + b'    opt:\n      - target=ts\n')
    assert _digest(root, 'python') == python, 'web/buf.gen.yaml moved the python digest'
    assert _digest(root, 'ts') != ts, 'web/buf.gen.yaml left the ts digest where it was'
    assert _digest(root, 'ts') == _expected(root, 'ts')


def test_buf_yaml_moves_the_ts_digest_and_not_the_python_one(tmp_path: Path):
    """The repo-root buf.yaml is an input of buf generate's module resolution (ts), and never of protoc."""
    root = _tree(tmp_path)
    python, ts = _digest(root, 'python'), _digest(root, 'ts')
    (root / 'buf.yaml').write_bytes(BUF_YAML_TEXT + b'    excludes:\n      - proto/trader_joe/proto/b\n')
    assert _digest(root, 'python') == python, 'buf.yaml moved the python digest'
    moved = _digest(root, 'ts')
    assert moved != ts, 'buf.yaml left the ts digest where it was'
    assert moved == _expected(root, 'ts')
    (root / 'buf.yaml').write_bytes(BUF_YAML_TEXT)
    assert _digest(root, 'ts') == ts, 'restoring buf.yaml did not restore the ts digest'


def test_the_ts_manifest_lists_buf_yaml_by_its_root_relative_path(tmp_path: Path):
    """buf.yaml is hashed under the name 'buf.yaml', sorted bytewise with the rest ('b' < 'p' < 'w')."""
    root = _tree(tmp_path)
    assert _inputs(root, 'ts')[0] == b'buf.yaml'
    assert _inputs(root, 'ts')[-1] == b'web/buf.gen.yaml'
    without_buf_yaml = MANIFEST_VERSION + b'\ntarget ts\n'
    for relative in _inputs(root, 'ts'):
        if relative != b'buf.yaml':
            content = (root / os.fsdecode(relative)).read_bytes()
            without_buf_yaml += b'f ' + hashlib.sha256(content).hexdigest().encode() + b' ' + relative + b'\n'
    assert _digest(root, 'ts') != hashlib.sha256(without_buf_yaml).hexdigest(), 'buf.yaml is not in the manifest'
    assert _digest(root, 'ts') == _expected(root, 'ts')


@pytest.mark.parametrize('target', ['python', 'ts'])
def test_the_manifest_format_is_v2(tmp_path: Path, target: str):
    """The version line reads v2 for both targets: the v1 manifest of the same files gives another digest."""
    assert SCRIPT.read_text(encoding='utf-8').count("MANIFEST_VERSION='trader_joe proto-digest v2'\n") == 1
    root = _tree(tmp_path)
    digest = _digest(root, target)
    assert digest == _expected(root, target, b'trader_joe proto-digest v2')
    assert digest != _expected(root, target, b'trader_joe proto-digest v1')


def test_python_needs_no_web_directory_and_no_buf_yaml(tmp_path: Path):
    root = _tree(tmp_path, buf_gen=None, buf_yaml=None)
    assert _digest(root, 'python') == _expected(root, 'python')
    _refused(_run(root, 'ts'))


def test_non_proto_files_under_proto_are_not_inputs(tmp_path: Path):
    root = _tree(tmp_path)
    before = _digest(root, 'python')
    (root / 'proto' / 'README.md').write_text('notes\n')
    (root / 'proto' / 'buf.lock').write_text('x\n')
    assert _digest(root, 'python') == before


def test_the_digest_is_independent_of_the_filesystem_walk_order(tmp_path: Path):
    """Two trees of the same files created in opposite orders; the listing order is not the sorted order."""
    names = [
        f'trader_joe/proto/m/v1/{chr(ord("a") + index % 26)}{index:02d}_{"Z" if index % 3 else "z"}.proto'
        for index in range(40)
    ]
    contents = {name: f'// {name}\n'.encode() for name in names}
    forward = _tree(tmp_path / 'forward', dict(contents))
    backward = _tree(tmp_path / 'backward', dict(reversed(list(contents.items()))))
    listing = os.listdir(forward / 'proto/trader_joe/proto/m/v1')
    listing_back = os.listdir(backward / 'proto/trader_joe/proto/m/v1')
    assert listing != sorted(listing) or listing_back != sorted(listing_back), (
        'the premise failed: both directories list in sorted order, so this test cannot see an unsorted walk'
    )
    assert _digest(forward, 'python') == _digest(backward, 'python') == _expected(forward, 'python')


def _installed_locales() -> list[str]:
    result = subprocess.run(['locale', '-a'], capture_output=True, text=True, check=False)
    return sorted(set(result.stdout.split()) | {'C'})


def test_the_digest_does_not_depend_on_the_callers_locale(tmp_path: Path):
    """Bytewise order whatever LC_ALL/LANG the caller has, over every locale this host has installed.

    Upper and lower case and a non-ASCII name sort differently under a language collation than under
    C. A host that has only C and C.UTF-8 (both bytewise) cannot show the difference, so the source
    pin below holds the line there.
    """
    files = {'trader_joe/proto/B.proto': b'b\n', 'trader_joe/proto/a.proto': b'a\n', 'trader_joe/proto/é.proto': b'e\n'}
    root = _tree(tmp_path, files)
    expected = _expected(root, 'python')
    for name in _installed_locales():
        env = _subprocess_env(LC_ALL=name, LANG=name, LC_COLLATE=name)
        assert _digest(root, 'python', env=env) == expected, f'the digest moved under locale {name}'


def test_the_script_sorts_under_lc_all_c_before_anything_sorts():
    """The source pin for the locale test: `export LC_ALL=C` precedes the first sort."""
    text = SCRIPT.read_text(encoding='utf-8')
    assert 'export LC_ALL=C\n' in text
    assert text.index('export LC_ALL=C\n') < text.index('sort -z')


def test_spaces_and_unicode_in_names_are_digested_by_name(tmp_path: Path):
    files = {'trader_joe/proto/a b/v1/c d.proto': b'x\n', 'trader_joe/proto/ü/v1/日本.proto': b'y\n'}
    root = _tree(tmp_path, files)
    spaced = _digest(root, 'python')
    assert spaced == _expected(root, 'python')
    path = root / 'proto' / 'trader_joe/proto/a b/v1/c d.proto'
    path.rename(path.with_name('c_d.proto'))
    assert _digest(root, 'python') != spaced


# ---------------------------------------------------------------------------------------------------
# THE SCRIPT: refusals print no digest and exit non-zero


def _symlinked_proto(root: Path) -> None:
    outside = root.parent / 'outside.proto'
    outside.write_bytes(b'syntax = "proto3";\n')
    (root / 'proto' / 'trader_joe/proto/a/v1/link.proto').symlink_to(outside)


def _symlinked_other_file(root: Path) -> None:
    (root / 'proto' / 'notes.md').symlink_to(root / 'tools' / 'proto_digest.sh')


def _symlinked_directory(root: Path) -> None:
    other = root.parent / 'other' / 'v1'
    other.mkdir(parents=True)
    (other / 'o.proto').write_bytes(b'syntax = "proto3";\n')
    (root / 'proto' / 'trader_joe/proto/other').symlink_to(other.parent)


def _proto_is_a_symlink(root: Path) -> None:
    real = root.parent / 'real_proto'
    (root / 'proto').rename(real)
    (root / 'proto').symlink_to(real)


def _proto_missing(root: Path) -> None:
    shutil.rmtree(root / 'proto')


def _fifo_proto(root: Path) -> None:
    os.mkfifo(root / 'proto' / 'trader_joe/proto/a/v1/pipe.proto')


def _newline_in_path(root: Path) -> None:
    (root / 'proto' / 'trader_joe/proto/a/v1/bad\nname.proto').write_bytes(b'syntax = "proto3";\n')


def _no_proto_at_all(root: Path) -> None:
    shutil.rmtree(root / 'proto')
    (root / 'proto' / 'trader_joe').mkdir(parents=True)
    (root / 'proto' / 'README.md').write_text('no protos\n')


REFUSALS = {
    'symlinked-proto': _symlinked_proto,
    'symlinked-non-proto': _symlinked_other_file,
    'symlinked-directory': _symlinked_directory,
    'proto-is-a-symlink': _proto_is_a_symlink,
    'proto-missing': _proto_missing,
    'fifo-named-proto': _fifo_proto,
    'newline-in-path': _newline_in_path,
    'empty-proto-set': _no_proto_at_all,
}


@pytest.mark.parametrize('target', ['python', 'ts'])
@pytest.mark.parametrize('breakage', REFUSALS.values(), ids=REFUSALS.keys())
def test_an_unsafe_or_empty_input_set_is_refused_with_no_digest(tmp_path: Path, breakage, target: str):
    root = _tree(tmp_path)
    breakage(root)
    _refused(_run(root, target))


def _buf_gen_symlink(root: Path) -> None:
    real = root.parent / 'buf.gen.yaml'
    (root / 'web' / 'buf.gen.yaml').rename(real)
    (root / 'web' / 'buf.gen.yaml').symlink_to(real)


def _web_symlink(root: Path) -> None:
    real = root.parent / 'real_web'
    (root / 'web').rename(real)
    (root / 'web').symlink_to(real)


def _buf_gen_missing(root: Path) -> None:
    (root / 'web' / 'buf.gen.yaml').unlink()


def _buf_gen_unreadable(root: Path) -> None:
    (root / 'web' / 'buf.gen.yaml').chmod(0)


def _buf_gen_not_regular(root: Path) -> None:
    (root / 'web' / 'buf.gen.yaml').unlink()
    os.mkfifo(root / 'web' / 'buf.gen.yaml')


def _unreadable_is_unreadable(path: Path) -> bool:
    """True when mode 0 really hides the file; root reads it anyway, and then the test skips, never passes."""
    return not os.access(path, os.R_OK)


@pytest.mark.parametrize(
    'breakage', [_buf_gen_symlink, _web_symlink, _buf_gen_missing, _buf_gen_unreadable, _buf_gen_not_regular]
)
def test_ts_refuses_a_linked_or_missing_buf_gen_yaml(tmp_path: Path, breakage):
    """The tree has buf.yaml, and ts digests it before the breakage: the refusal is web/buf.gen.yaml's own."""
    root = _tree(tmp_path)
    assert _digest(root, 'ts') == _expected(root, 'ts'), 'the premise failed: ts refused an intact tree'
    breakage(root)
    if breakage is _buf_gen_unreadable and not _unreadable_is_unreadable(root / 'web' / 'buf.gen.yaml'):
        pytest.skip('running as a user that reads mode-0 files')
    result = _run(root, 'ts')
    _refused(result)
    # The script's own refusal, not a shell error from reading the file later ('...: Permission denied').
    assert 'web/buf.gen.yaml is' in result.stderr, result.stderr
    assert _digest(root, 'python') == _expected(root, 'python'), 'python reads web/ too'


def _buf_yaml_missing(root: Path) -> None:
    (root / 'buf.yaml').unlink()


def _buf_yaml_unreadable(root: Path) -> None:
    (root / 'buf.yaml').chmod(0)


def _buf_yaml_symlink(root: Path) -> None:
    real = root.parent / 'real_buf.yaml'
    (root / 'buf.yaml').rename(real)
    (root / 'buf.yaml').symlink_to(real)


def _buf_yaml_dangling_symlink(root: Path) -> None:
    (root / 'buf.yaml').unlink()
    (root / 'buf.yaml').symlink_to(root.parent / 'nowhere.yaml')


def _buf_yaml_directory(root: Path) -> None:
    (root / 'buf.yaml').unlink()
    (root / 'buf.yaml').mkdir()


def _buf_yaml_fifo(root: Path) -> None:
    (root / 'buf.yaml').unlink()
    os.mkfifo(root / 'buf.yaml')


BUF_YAML_REFUSALS = {
    'missing': _buf_yaml_missing,
    'unreadable': _buf_yaml_unreadable,
    'symlink': _buf_yaml_symlink,
    'dangling-symlink': _buf_yaml_dangling_symlink,
    'directory': _buf_yaml_directory,
    'fifo': _buf_yaml_fifo,
}


@pytest.mark.parametrize('breakage', BUF_YAML_REFUSALS.values(), ids=BUF_YAML_REFUSALS.keys())
def test_ts_refuses_a_missing_unreadable_linked_or_irregular_buf_yaml(tmp_path: Path, breakage):
    """No digest and a non-zero exit for ts, naming buf.yaml; python still digests, since protoc never reads it."""
    root = _tree(tmp_path)
    assert _digest(root, 'ts') == _expected(root, 'ts'), 'the premise failed: ts refused an intact tree'
    breakage(root)
    if breakage is _buf_yaml_unreadable and not _unreadable_is_unreadable(root / 'buf.yaml'):
        pytest.skip('running as a user that reads mode-0 files')
    result = _run(root, 'ts')
    _refused(result)
    # The script's own refusal, not a shell error from reading the file later ('...: Permission denied').
    assert ': buf.yaml is' in result.stderr, f'the refusal does not name buf.yaml: {result.stderr!r}'
    assert _digest(root, 'python') == _expected(root, 'python'), 'python depends on buf.yaml'


@pytest.mark.parametrize('arguments', [(), ('go',), ('python', 'ts'), ('',)])
def test_a_bad_argument_list_is_refused(tmp_path: Path, arguments: tuple[str, ...]):
    _refused(_run(_tree(tmp_path), *arguments))


def test_the_script_leaves_no_temporary_files(tmp_path: Path):
    root = _tree(tmp_path)
    scratch = tmp_path / 'tmp'
    scratch.mkdir()
    env = _subprocess_env(TMPDIR=str(scratch))
    _digest(root, 'ts', env=env)
    _newline_in_path(root)
    _refused(_run(root, 'python', env=env))
    assert list(scratch.iterdir()) == []


# ---------------------------------------------------------------------------------------------------
# THE SCRIPT: ts refuses buf configuration under proto/ (bead tj-grna9p.187)
#
# `buf generate ../proto` (run from web/) obeys a buf.yaml, buf.work.yaml or buf.lock found at any depth
# under proto/, and the digest hashes none of them, so one would change generation without moving the ts
# digest. The ts target refuses: exit 4 (the script's code for an unsafe input set), nothing on stdout,
# the script's own message and the offending path, one per line, on stderr. Any entry type is refused
# (file, directory, fifo, symlink, dangling symlink): the match is by name, exactly and case-sensitively.
# python is unaffected, since protoc reads none of them; its own symlink rule still applies to a link.

BUF_CONFIG_NAMES = ['buf.yaml', 'buf.work.yaml', 'buf.lock']
BUF_CONFIG_PLACES = {'top-level': '', 'nested': 'trader_joe/proto/ui/v1'}
BUF_CONFIG_REFUSAL = 'refusing the ts digest; buf reads these under proto/'
SYMLINK_REFUSAL = 'symlinks under proto/ are not followed'


def _buf_config_refused(result: subprocess.CompletedProcess, *offenders: str) -> None:
    """The buf-config refusal itself (not another one): exit 4, empty stdout, the message and each path on its own line."""
    _refused(result)
    assert result.returncode == 4, f'exit {result.returncode}, not the refusal code 4: {result.stderr!r}'
    assert BUF_CONFIG_REFUSAL in result.stderr, f'not the buf-config refusal: {result.stderr!r}'
    lines = result.stderr.splitlines()
    for offender in offenders:
        assert offender in lines, f'the refusal does not name {offender!r} on a line of its own: {result.stderr!r}'


def _intact(root: Path) -> tuple[str, str]:
    """The premise: both targets digest the tree before the buf file is added. Returns (python, ts)."""
    python, ts = _digest(root, 'python'), _digest(root, 'ts')
    assert (python, ts) == (_expected(root, 'python'), _expected(root, 'ts')), 'the premise failed'
    return python, ts


@pytest.mark.parametrize('place', BUF_CONFIG_PLACES.values(), ids=BUF_CONFIG_PLACES.keys())
@pytest.mark.parametrize('name', BUF_CONFIG_NAMES)
def test_ts_refuses_a_buf_config_file_under_proto_and_python_still_digests(tmp_path: Path, name: str, place: str):
    root = _tree(tmp_path)
    python, _ = _intact(root)
    directory = root / 'proto' / place
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(b'version: v2\n')
    offender = str(Path('proto', place, name))
    _buf_config_refused(_run(root, 'ts'), offender)
    assert _digest(root, 'python') == python == _expected(root, 'python'), f'{offender} moved or broke python'


def test_ts_names_every_offender_and_a_path_with_spaces_and_unicode(tmp_path: Path):
    root = _tree(tmp_path)
    _intact(root)
    odd = root / 'proto' / 'trader_joe/a b/é'
    odd.mkdir(parents=True)
    (odd / 'buf.yaml').write_bytes(b'version: v2\n')
    (root / 'proto' / 'buf.lock').write_bytes(b'version: v2\n')
    _buf_config_refused(_run(root, 'ts'), 'proto/trader_joe/a b/é/buf.yaml', 'proto/buf.lock')


def _buf_lock_symlink_to_a_file(root: Path) -> None:
    real = root.parent / 'real_buf.lock'
    real.write_bytes(b'version: v2\n')
    (root / 'proto' / 'buf.lock').symlink_to(real)


def _buf_lock_dangling_symlink(root: Path) -> None:
    (root / 'proto' / 'trader_joe' / 'buf.lock').symlink_to(root.parent / 'nowhere.lock')


@pytest.mark.parametrize('breakage', [_buf_lock_symlink_to_a_file, _buf_lock_dangling_symlink])
def test_ts_refuses_a_symlink_named_buf_lock_as_buf_config(tmp_path: Path, breakage):
    """Refused by the buf-config check by name, before the general symlink rule; python refuses it as a link."""
    root = _tree(tmp_path)
    _intact(root)
    breakage(root)
    offender = next(str(path.relative_to(root)) for path in (root / 'proto').rglob('buf.lock'))
    _buf_config_refused(_run(root, 'ts'), offender)
    python = _run(root, 'python')
    _refused(python)
    assert SYMLINK_REFUSAL in python.stderr, f'python refused it for another reason: {python.stderr!r}'


def _buf_yaml_directory_under_proto(root: Path) -> str:
    (root / 'proto' / 'buf.yaml').mkdir()
    return 'proto/buf.yaml'


def _buf_work_fifo_under_proto(root: Path) -> str:
    os.mkfifo(root / 'proto' / 'trader_joe' / 'buf.work.yaml')
    return 'proto/trader_joe/buf.work.yaml'


@pytest.mark.parametrize('breakage', [_buf_yaml_directory_under_proto, _buf_work_fifo_under_proto])
def test_ts_refuses_a_buf_config_name_whatever_the_entry_type(tmp_path: Path, breakage):
    """A DIRECTORY named buf.yaml is refused too (the match is by name, any type); so is a fifo. python digests."""
    root = _tree(tmp_path)
    python, _ = _intact(root)
    offender = breakage(root)
    _buf_config_refused(_run(root, 'ts'), offender)
    assert _digest(root, 'python') == python


LOOKALIKES = [
    'notbuf.yaml',
    'buf.yaml.bak',
    'BUF.YAML',
    'Buf.lock',
    'buf.lock~',
    'buf.work.yml',
    'buf.yml',
    'xbuf.lock',
]


def test_names_merely_containing_a_buf_config_name_are_not_refused(tmp_path: Path):
    """An exact, case-sensitive match only: look-alikes are neither refused nor inputs (both digests unchanged)."""
    root = _tree(tmp_path)
    python, ts = _intact(root)
    nested = root / 'proto' / 'trader_joe/proto/ui/v1'
    nested.mkdir(parents=True)
    for name in LOOKALIKES:
        (root / 'proto' / name).write_bytes(b'version: v2\n')
        (nested / name).write_bytes(b'version: v2\n')
    assert (_digest(root, 'python'), _digest(root, 'ts')) == (python, ts)


def test_buf_files_outside_proto_are_not_refused(tmp_path: Path):
    """The refusal is proto/'s only.

    The repo-root buf.yaml and web/buf.gen.yaml are inputs (the intact tree digests), and a buf.lock at the
    root or in web/ is neither refused nor hashed -- the documented known limit (buf.lock becomes an input
    only if buf.yaml declares dependencies).
    """
    root = _tree(tmp_path)
    python, ts = _intact(root)
    (root / 'buf.lock').write_bytes(b'version: v2\n')
    (root / 'web' / 'buf.lock').write_bytes(b'version: v2\n')
    (root / 'web' / 'buf.yaml').write_bytes(BUF_YAML_TEXT)
    assert (_digest(root, 'python'), _digest(root, 'ts')) == (python, ts)


def test_the_real_repository_has_no_buf_config_under_proto_and_digests_for_ts():
    """The real tree is valid for the refusal: no buf config under proto/, a root buf.yaml, a ts digest."""
    found = [path for name in BUF_CONFIG_NAMES for path in (REPO_ROOT / 'proto').rglob(name)]
    assert found == [], f'buf configuration under proto/: {found}'
    assert (REPO_ROOT / 'buf.yaml').is_file() and BUF_GEN.is_file()
    assert _digest(REPO_ROOT, 'ts') == _expected(REPO_ROOT, 'ts')


def _header() -> str:
    """The script's leading comment block, its lines joined with single spaces."""
    lines = SCRIPT.read_text(encoding='utf-8').splitlines()[1:]
    comment = []
    for line in lines:
        if line.startswith('#'):
            comment.append(line.lstrip('#').strip())
        elif comment and line.startswith('MANIFEST_VERSION'):
            break
    return ' '.join(part for part in comment if part)


def test_the_header_records_the_buf_config_refusal_and_the_known_limits():
    header = _header()
    assert 'For ts, a buf.yaml, buf.work.yaml or buf.lock ANYWHERE under proto/' in header
    assert 'the digests of valid trees are byte-identical to before it' in header
    assert 'KNOWN LIMITS. The stamp answers "generated from these protos and this config".' in header
    assert "python's protoc flags (the Makefile's proto recipe)" in header
    assert 'they are deliberately outside the digest' in header
    assert 'buf.lock becomes an input if buf.yaml ever declares dependencies' in header


# ---------------------------------------------------------------------------------------------------
# THE MAKE TARGETS: the real recipes, from a scratch tree, with protoc and npm stood in

# Stands in for `uv run python -m grpc_tools.protoc ...`: records whether the stamp exists while
# protoc "runs", then exits UV_EXIT.
UV_PROBE = r"""#!/bin/sh
if [ -e "$STAMP_PROBE" ]; then echo present >> "$PROBE_LOG"; else echo absent >> "$PROBE_LOG"; fi
exit "${UV_EXIT:-0}"
"""
# Stands in for npm: `--prefix web run gen:proto` runs the REAL web/scripts/gen-proto.sh exactly as
# package.json's gen:proto does (`sh scripts/gen-proto.sh` from web/), so the stamp the target leaves is
# the script's. NPM_MODE=die-before-script fails as npm would before reaching the script (a broken
# install, a missing script), recording the stamp's state first.
NPM_PROBE = r"""#!/bin/sh
if [ "$*" = "--prefix web run gen:proto" ]; then
    if [ "${NPM_MODE:-}" = die-before-script ]; then
        if [ -e "$STAMP_PROBE" ]; then echo npm-present >> "$PROBE_LOG"; else echo npm-absent >> "$PROBE_LOG"; fi
        exit 1
    fi
    cd web && exec sh scripts/gen-proto.sh
fi
exit 0
"""
# Stands in for buf (run by gen-proto.sh from web/): records whether the stamp exists while it "runs",
# regenerates gen/proto/ts as clean: true would (remove, then write), and exits BUF_EXIT.
BUF_PROBE = r"""#!/bin/sh
if [ -e "$STAMP_PROBE" ]; then echo buf-present >> "$PROBE_LOG"; else echo buf-absent >> "$PROBE_LOG"; fi
rm -rf ../gen/proto/ts
mkdir -p ../gen/proto/ts
echo generated > ../gen/proto/ts/a_pb.ts
exit "${BUF_EXIT:-0}"
"""
# Wraps the real digest script (moved beside it as proto_digest.real.sh) and logs every call, so a
# second writer of the ts stamp shows up as a second digest call even though it writes the same bytes.
DIGEST_PROBE = r"""#!/bin/bash
echo "digest-$*" >> "$PROBE_LOG"
exec bash "$(dirname "$0")/proto_digest.real.sh" "$@"
"""
STALE = 'stale-stamp-from-an-earlier-generation\n'


def _probe_lines(tmp_path: Path) -> list[str]:
    log = tmp_path / 'probe.log'
    return log.read_text().split() if log.exists() else []


def _make_proto(tmp_path: Path, root: Path, *arguments: str, stamp: Path | None = None, **values: str):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir(exist_ok=True)
    _executable(bin_dir / 'uv', UV_PROBE)
    env = _subprocess_env(
        PATH=f'{bin_dir}{os.pathsep}{os.environ["PATH"]}',
        STAMP_PROBE=str(stamp or root / PYTHON_STAMP),
        PROBE_LOG=str(tmp_path / 'probe.log'),
        **values,
    )
    return _run_make(root, 'proto', *arguments, env=env)


def _seed(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(STALE)


def test_make_proto_deletes_the_old_stamp_first_and_writes_the_digest_last(tmp_path: Path):
    root = _tree(tmp_path)
    _seed(root / PYTHON_STAMP)
    result = _make_proto(tmp_path, root)
    assert result.returncode == 0, result.stderr
    assert _probe_lines(tmp_path) == ['absent'], 'the stamp existed while protoc ran'
    assert (root / PYTHON_STAMP).read_text() == _digest(root, 'python') + '\n'


def test_make_proto_writes_the_stamp_on_a_fresh_tree(tmp_path: Path):
    root = _tree(tmp_path)
    assert _make_proto(tmp_path, root).returncode == 0
    assert (root / PYTHON_STAMP).read_text() == _expected(root, 'python') + '\n'


def test_a_failed_protoc_fails_make_proto_and_leaves_no_stamp(tmp_path: Path):
    root = _tree(tmp_path)
    _seed(root / PYTHON_STAMP)
    result = _make_proto(tmp_path, root, UV_EXIT='1')
    assert result.returncode != 0, 'a failed protoc did not fail make proto'
    assert _probe_lines(tmp_path) == ['absent']
    assert not (root / PYTHON_STAMP).exists(), 'a failed generation left a stamp'


def test_a_failed_digest_fails_make_proto_and_writes_no_empty_stamp(tmp_path: Path):
    root = _tree(tmp_path)
    _seed(root / PYTHON_STAMP)
    _executable(root / 'tools' / 'proto_digest.sh', '#!/bin/sh\necho refused >&2\nexit 4\n')
    result = _make_proto(tmp_path, root)
    assert result.returncode != 0, 'a failed digest did not fail make proto'
    assert not (root / PYTHON_STAMP).exists(), 'a failed digest left a stamp'


def test_a_scratch_proto_src_writes_no_stamp_and_removes_its_old_one(tmp_path: Path):
    """The script digests the repository's proto/, so a stamp beside a tree built from elsewhere would lie."""
    root = _tree(tmp_path)
    scratch = tmp_path / 'scratch'
    (scratch / 'proto' / 'trader_joe/proto/s/v1').mkdir(parents=True)
    (scratch / 'proto' / 'trader_joe/proto/s/v1/s.proto').write_bytes(b'syntax = "proto3";\n')
    gen = scratch / 'gen' / 'proto' / 'python'
    scratch_stamp = scratch / 'gen' / 'proto' / 'python.digest'
    _seed(scratch_stamp)
    _seed(root / PYTHON_STAMP)
    result = _make_proto(tmp_path, root, f'PROTO_SRC={scratch / "proto"}', f'PROTO_GEN={gen}', stamp=scratch_stamp)
    assert result.returncode == 0, result.stderr
    assert _probe_lines(tmp_path) == ['absent']
    assert not scratch_stamp.exists(), 'a scratch PROTO_SRC left a stamp beside its tree'
    assert (root / PYTHON_STAMP).read_text() == STALE, "a scratch run touched the repository tree's stamp"


def _ts_tree(tmp_path: Path) -> Path:
    """The scratch tree plus the real web/scripts/gen-proto.sh, with the digest script wrapped to log its calls."""
    root = _tree(tmp_path)
    (root / 'web' / 'scripts').mkdir(parents=True)
    shutil.copy2(GEN_SCRIPT, root / 'web' / 'scripts' / 'gen-proto.sh')
    tools = root / 'tools'
    (tools / 'proto_digest.sh').rename(tools / 'proto_digest.real.sh')
    _executable(tools / 'proto_digest.sh', DIGEST_PROBE)
    return root


def _make_ts(tmp_path: Path, root: Path, **values: str):
    (root / 'web' / 'node_modules').mkdir(parents=True, exist_ok=True)
    stubs = Stubs(tmp_path).install_node()
    _executable(stubs.bin / 'npm', NPM_PROBE)
    _executable(stubs.bin / 'buf', BUF_PROBE)
    env = stubs.env(STAMP_PROBE=str(root / TS_STAMP), PROBE_LOG=str(tmp_path / 'probe.log'), **values)
    return _run_make(root, 'gen-proto-ts', env=env)


def _ts_digest(root: Path) -> str:
    """The real script's digest of the scratch tree's ts inputs, unlogged."""
    result = subprocess.run(
        ['bash', str(root / 'tools' / 'proto_digest.real.sh'), 'ts'],
        capture_output=True, text=True, env=_subprocess_env(), timeout=TIMEOUT_S, check=False,
    )  # fmt: skip
    assert result.returncode == 0 and DIGEST_LINE.fullmatch(result.stdout), result
    return result.stdout.strip()


def test_make_gen_proto_ts_stamps_through_the_script_alone(tmp_path: Path):
    """Old stamp gone before buf runs, ONE digest call (the script's), the stamp the documented digest."""
    root = _ts_tree(tmp_path)
    _seed(root / TS_STAMP)
    result = _make_ts(tmp_path, root)
    assert result.returncode == 0, result.stderr
    assert _probe_lines(tmp_path) == ['buf-absent', 'digest-ts'], 'not: stamp gone, buf, then exactly one digest'
    assert (root / TS_STAMP).read_text() == _ts_digest(root) + '\n'
    assert (root / TS_STAMP).read_text() == _expected(root, 'ts') + '\n'
    assert sorted(path.name for path in (root / 'gen' / 'proto').iterdir()) == ['ts', 'ts.digest']


def test_make_gen_proto_ts_writes_the_stamp_on_a_fresh_tree(tmp_path: Path):
    root = _ts_tree(tmp_path)
    result = _make_ts(tmp_path, root)
    assert result.returncode == 0, result.stderr
    assert (root / TS_STAMP).read_text() == _expected(root, 'ts') + '\n'


def test_a_failed_ts_generation_fails_the_target_and_leaves_no_stamp(tmp_path: Path):
    root = _ts_tree(tmp_path)
    _seed(root / TS_STAMP)
    result = _make_ts(tmp_path, root, BUF_EXIT='1')
    assert result.returncode != 0, 'a failed buf generate did not fail make gen-proto-ts'
    assert _probe_lines(tmp_path) == ['buf-absent'], 'something digested after a failed generation'
    assert not (root / TS_STAMP).exists(), 'a failed generation left a stamp'


def test_npm_failing_before_the_script_still_clears_the_stale_stamp(tmp_path: Path):
    """The recipe's own rm: npm may die before gen-proto.sh runs, and the old stamp must not survive that."""
    root = _ts_tree(tmp_path)
    _seed(root / TS_STAMP)
    result = _make_ts(tmp_path, root, NPM_MODE='die-before-script')
    assert result.returncode != 0, 'a failed npm did not fail make gen-proto-ts'
    assert _probe_lines(tmp_path) == ['npm-absent'], 'the stale stamp was still there when npm ran'
    assert not (root / TS_STAMP).exists(), 'a stale stamp survived a generation that never ran'


def test_regenerating_the_ts_tree_keeps_the_stamp_beside_it(tmp_path: Path):
    """The buf stand-in cleans gen/proto/ts as clean: true does; the stamp the script wrote survives that."""
    root = _ts_tree(tmp_path)
    _seed(root / 'gen' / 'proto' / 'ts' / 'old_pb.ts')
    assert _make_ts(tmp_path, root).returncode == 0
    assert not (root / 'gen' / 'proto' / 'ts' / 'old_pb.ts').exists(), 'buf did not clean the tree'
    assert (root / 'gen' / 'proto' / 'ts' / 'a_pb.ts').exists(), 'buf did not regenerate the tree'
    assert (root / TS_STAMP).read_text() == _expected(root, 'ts') + '\n'
    shutil.rmtree(root / 'gen' / 'proto' / 'ts')
    assert (root / TS_STAMP).read_text() == _expected(root, 'ts') + '\n'


def _make_value(name: str) -> str:
    result = subprocess.run(
        ['make', '-s', '--no-print-directory', '-C', str(REPO_ROOT), '-f', str(MAKEFILE),
         '--eval', 'print-value-%: ; @: $(info $($*))', f'print-value-{name}'],
        capture_output=True, text=True, env=_subprocess_env(), check=False, timeout=TIMEOUT_S,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_the_stamps_sit_outside_the_trees_their_generators_clear():
    python_stamp, python_tree = Path(_make_value('PROTO_STAMP')), Path(_make_value('PROTO_GEN'))
    assert (python_stamp, python_tree) == (PYTHON_STAMP, Path('gen/proto/python'))
    assert not python_stamp.is_relative_to(python_tree)
    assert Path(_make_value('TS_STAMP')) == TS_STAMP
    buf_gen = BUF_GEN.read_text(encoding='utf-8')
    assert re.search(r'^clean: true$', buf_gen, re.MULTILINE), 'buf.gen.yaml no longer cleans its out tree'
    outs = re.findall(r'^\s+out:\s*(\S+)\s*$', buf_gen, re.MULTILINE)
    assert outs, 'buf.gen.yaml names no out directory'
    for out in outs:
        cleaned = Path(os.path.normpath(Path('web') / out))
        assert cleaned == Path('gen/proto/ts')
        assert not TS_STAMP.is_relative_to(cleaned), f'{TS_STAMP} sits inside {cleaned}, which buf cleans'


def test_gen_is_not_committed():
    ignored = subprocess.run(
        ['git', 'check-ignore', '--', str(PYTHON_STAMP), str(TS_STAMP)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ignored.stdout.split() == [str(PYTHON_STAMP), str(TS_STAMP)], f'a stamp is not gitignored: {ignored}'


# ---------------------------------------------------------------------------------------------------
# THE MAKE TARGETS: who writes the stamps, and in what order


def _recipe_lines_writing_a_stamp() -> list[str]:
    return [
        line
        for line in MAKEFILE.read_text(encoding='utf-8').splitlines()
        if 'proto_digest.sh' in line and line.startswith('\t')
    ]


def test_only_make_proto_runs_the_digest_script():
    """One Makefile call site, the python stamp; the ts stamp's one writer is gen-proto.sh (tj-grna9p.188).

    No npm script names the digest either: gen:proto runs gen-proto.sh, which digests (tj-grna9p.186).
    """
    lines = _recipe_lines_writing_a_stamp()
    assert len(lines) == 1, lines
    assert 'proto_digest.sh python' in lines[0] and '$(PROTO_STAMP)' in lines[0], lines[0]
    assert _make_recipe('proto')[-1] == lines[0].strip().lstrip('@'), "the digest is not proto's last step"
    assert not any('proto_digest' in line for line in _make_recipe('gen-proto-ts'))
    scripts = PACKAGE_JSON.read_text(encoding='utf-8')
    assert 'digest' not in scripts, 'an npm script touches the content stamp'
    assert 'digest' not in BUF_GEN.read_text(encoding='utf-8')
    assert 'bash ../tools/proto_digest.sh ts' in GEN_SCRIPT.read_text(encoding='utf-8')


def test_the_makefile_never_writes_the_ts_stamp():
    """The only recipe line naming TS_STAMP, or its path, is gen-proto-ts's delete."""
    text = MAKEFILE.read_text(encoding='utf-8').replace('\\\n', ' ')
    naming = [
        line.strip().lstrip('@-+').strip()
        for line in text.splitlines()
        if line.startswith('\t') and ('TS_STAMP' in line or 'ts.digest' in line)
    ]
    assert naming == ['rm -f "$(TS_STAMP)"'], naming


def test_each_target_deletes_its_stamp_before_generating():
    proto = _make_recipe('proto')
    assert proto[0] == 'rm -f "$(PROTO_STAMP)"', f"proto's first step does not delete the stamp: {proto[0]!r}"
    ts = _make_recipe('gen-proto-ts')
    assert ts[-2:] == ['rm -f "$(TS_STAMP)"', '$(WEB_NPM) run gen:proto'], ts


# ---------------------------------------------------------------------------------------------------
# THE MAKEFILE COMMENTS (bead tj-grna9p.187): source checks of what they claim


def _makefile_comment(start: str, end: str) -> str:
    """The Makefile comment from the line containing `start` up to the first line starting with `end`, joined."""
    lines = MAKEFILE.read_text(encoding='utf-8').splitlines()
    first = next(index for index, line in enumerate(lines) if start in line)
    block = []
    for line in lines[first:]:
        if line.startswith(end):
            break
        block.append(line.lstrip('#').strip())
    return ' '.join(part for part in block if part)


def _sentences(text: str) -> list[str]:
    return [sentence for sentence in re.split(r'(?<=[.])\s+', text) if sentence]


def test_the_host_wide_names_comment_calls_the_store_api_network_shared_and_unproven():
    """Container names and published ports clash for certain; trader_joe_store_api is shared, its clash UNPROVEN.

    ADR tj-4rr0la addendum 30: the network is never listed among the certain clashes.
    """
    text = _makefile_comment('THE HAZARD THAT REMAINS', 'DEV_PROJECT :=')
    sentences = _sentences(text)
    certain = [sentence for sentence in sentences if 'clash for certain' in sentence]
    assert certain, f'no sentence states the certain clashes: {text!r}'
    before = text[: text.index(certain[0])]
    assert 'container names' in before and 'published ports' in before, before
    assert 'trader_joe_store_api' not in before + certain[0], 'the store API network is listed as a certain clash'
    network = [sentence for sentence in sentences if 'trader_joe_store_api' in sentence]
    assert len(network) == 1, network
    assert 'SHARED' in network[0] and 'UNPROVEN' in network[0], network[0]
    compose = (REPO_ROOT / 'docker-compose.yaml').read_text(encoding='utf-8')
    assert 'name: ${STORE_API_NETWORK:-trader_joe_store_api}' in compose, 'the network the comment names moved'
    assert 'container_name: ${DATABASE_NAME}' in compose, 'the container name the comment names moved'


def test_the_ts_stamp_comment_says_every_ts_generation_stamps():
    text = _makefile_comment('The TS twin of the Python content stamp', 'TS_STAMP :=')
    assert 'Every ts generation stamps, make or npm' in text, text
    assert 'there is ONE writer: web/scripts/gen-proto.sh' in text, text
    assert 'written twice' not in text, 'the retired double-write claim is back'
    assert 'deletes the stamp, runs buf generate, and writes the stamp last via tools/proto_digest.sh ts' in text
    assert 'without touching it' not in text, 'the retired claim that npm leaves the stamp alone is back'
    gen_proto = (REPO_ROOT / 'web' / 'scripts' / 'gen-proto.sh').read_text(encoding='utf-8')
    assert 'tools/proto_digest.sh ts' in gen_proto, 'gen-proto.sh no longer stamps through the script'
