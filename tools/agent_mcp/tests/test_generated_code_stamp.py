"""The generated-code guard reads the content stamp, not mtimes (bead tj-iee6sn.33, design tj-grna9p.164).

Design checked against: tj-grna9p.164 (one algorithm, tools/proto_digest.sh, for writer and reader; the
stamp is gen/proto/<kind>.digest, a sibling of the tree) and the tj-iee6sn.33 description and architect
note. stack.check_generated_code_fresh, for each generated tree in order:

    tree missing or holding no regular file (no link followed)   -> '<tree> is missing or empty: <hint>'
    stamp missing, a link, not regular or unreadable (no-follow) -> 'no generation stamp: ...: <hint>'
    the BAKED script fails, times out or prints anything but one 64-hex line
                                                                  -> 'the <kind> proto digest could not be computed (...): <hint>'
    stamp != the baked script's digest over the repository now  -> 'generated code is not from the current protos: <tree>: <hint>'

The digest is computed by the TRUSTED copy baked into the MCP image (/opt/agent_mcp/compose/tools/
proto_digest.sh), run as `bash <trusted> --context <repo_root> <kind>`, never by the checkout's own copy,
which an agent can write. mtime is not read at all: a touch changes nothing, an edit that keeps the mtime
is still caught, and identical content under an old mtime passes (the bug that started this).

The script runs for real here, a copy of the repository's standing in for the baked one. The --context
flag of the script itself is pinned at the bottom; its default (no flag) behaviour keeps the pins of
server/common/tests/test_proto_digest_make.py, which compare it with the documented manifest.
"""

import inspect
import os
import shlex
import shutil
import subprocess  # nosec B404 -- the repository's own script, argument lists, no shell
import time
from pathlib import Path

import pytest

from common.tests.test_proto_digest_make import _expected, _tree
from tools.agent_mcp import stack
from tools.agent_mcp.tests.harness import (
    REAL_PROTO_DIGEST,
    REPO_ROOT,
    FakeDocker,
    make_rig,
    run_proto_digest,
    write_generation_stamps,
)


pytestmark = pytest.mark.build_infra

TRUSTED_SCRIPT = '/opt/agent_mcp/compose/tools/proto_digest.sh'
HINT = 'run make proto gen-proto-ts in the root checkout'
TEST_HINT = 'run make proto in the root checkout'
TREES = ('gen/proto/python', 'gen/proto/ts')
KINDS = ('python', 'ts')
NO_STAMP = 'no generation stamp: {tree}.digest is missing, unreadable or not a regular file: ' + HINT
NOT_CURRENT = 'generated code is not from the current protos: {tree}: ' + HINT
MISSING = '{tree} is missing or empty: ' + HINT
A_PROTO = 'proto/trader_joe/proto/a/v1/a.proto'


# ---------------------------------------------------------------------------------------------------
# HELPERS


def _repo(tmp_path: Path) -> Path:
    """A scratch checkout (the real script, two protos, buf.yaml, web/buf.gen.yaml), both trees, both stamps."""
    repo = _tree(tmp_path)
    for relative, text in (
        ('gen/proto/python/trader_joe/proto/a/v1/a_pb2.py', 'DESCRIPTOR = None\n'),
        ('gen/proto/ts/trader_joe/proto/a/v1/a_pb.ts', 'export {};\n'),
    ):
        (repo / relative).parent.mkdir(parents=True, exist_ok=True)
        (repo / relative).write_text(text)
    write_generation_stamps(repo, repo / 'tools' / 'proto_digest.sh')
    return repo


def _trusted(tmp_path: Path, text: str | None = None) -> Path:
    """The stand-in for the baked script: the real one (or TEXT) at <tmp>/trusted/tools/proto_digest.sh."""
    script = tmp_path / 'trusted' / 'tools' / 'proto_digest.sh'
    script.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REAL_PROTO_DIGEST, script.parent / 'proto_digest.real.sh')
    if text is None:
        shutil.copy2(REAL_PROTO_DIGEST, script)
    else:
        script.write_text(text)
    return script


def _stub(kind: str, stdout: bytes, stderr: bytes, status: int) -> str:
    """A trusted script answering STDOUT/STDERR/STATUS for KIND, the real script for the other kind.

    It also leaves a `ran` marker beside itself, so a test can show the script was or was not run.
    """
    return (
        '#! /bin/bash\n'
        'here="$(dirname "$0")"\n'
        ': > "$here/ran"\n'
        f'if [ "${{3:-}}" = {kind} ]; then\n'
        f'    printf %s {shlex.quote(stdout.decode())}\n'
        f'    printf %s {shlex.quote(stderr.decode())} >&2\n'
        f'    exit {status}\n'
        'fi\n'
        'exec /usr/bin/bash "$here/proto_digest.real.sh" "$@"\n'
    )


def _check(repo: Path, script: Path, trees=TREES, hint: str = HINT) -> None:
    stack.check_generated_code_fresh(repo, trees, hint, trusted_script=script)


def _refusal(repo: Path, script: Path, trees=TREES, hint: str = HINT) -> str:
    with pytest.raises(stack.Refused) as refused:
        _check(repo, script, trees, hint)
    return str(refused.value)


def _stamp(repo: Path, kind: str) -> Path:
    return repo / 'gen' / 'proto' / f'{kind}.digest'


def _real_digest(repo: Path, kind: str) -> bytes:
    done = run_proto_digest(REAL_PROTO_DIGEST, repo, kind)
    assert done.returncode == 0, done.stderr
    return done.stdout


def _set_mtimes(root: Path, when: float) -> None:
    for directory, _, names in os.walk(root):
        for name in names:
            os.utime(Path(directory) / name, (when, when), follow_symlinks=False)


# ---------------------------------------------------------------------------------------------------
# THE STAMP THAT PASSES, AND EVERY STAMP THAT DOES NOT


def test_stamps_written_by_the_script_pass(tmp_path: Path):
    """Non-vacuity for every refusal below: the fixture as built passes, both trees, both kinds."""
    repo = _repo(tmp_path)
    _check(repo, _trusted(tmp_path))
    for kind in KINDS:
        assert _stamp(repo, kind).read_bytes() == _expected(repo, kind).encode() + b'\n'


def _missing(stamp: Path, tmp_path: Path) -> None:
    stamp.unlink()


def _a_link_to_the_right_digest(stamp: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / f'elsewhere-{stamp.name}'
    elsewhere.write_bytes(stamp.read_bytes())
    stamp.unlink()
    stamp.symlink_to(elsewhere)


def _a_directory(stamp: Path, tmp_path: Path) -> None:
    stamp.unlink()
    stamp.mkdir()


def _a_fifo(stamp: Path, tmp_path: Path) -> None:
    stamp.unlink()
    os.mkfifo(stamp)


def _unreadable(stamp: Path, tmp_path: Path) -> None:
    stamp.chmod(0o000)


def _a_dangling_link(stamp: Path, tmp_path: Path) -> None:
    stamp.unlink()
    stamp.symlink_to(tmp_path / 'nowhere.digest')


_NO_STAMP = {
    'missing': _missing,
    'a symlink to a file holding the right digest': _a_link_to_the_right_digest,
    'a dangling symlink': _a_dangling_link,
    'a directory': _a_directory,
    'a fifo (must not hang)': _a_fifo,
    'unreadable': _unreadable,
}


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('make_bad', list(_NO_STAMP.values()), ids=list(_NO_STAMP))
def test_a_stamp_that_cannot_be_read_without_following_a_link_refuses_naming_the_fix(
    tmp_path: Path, kind: str, make_bad
):
    if make_bad is _unreadable and os.geteuid() == 0:
        pytest.skip('root reads a mode-000 file')
    repo = _repo(tmp_path)
    make_bad(_stamp(repo, kind), tmp_path)
    assert _refusal(repo, _trusted(tmp_path)) == NO_STAMP.format(tree=f'gen/proto/{kind}')


def _other_kind(kind: str) -> str:
    return 'ts' if kind == 'python' else 'python'


_WRONG_CONTENT = {
    'another digest': lambda repo, kind: b'0' * 64 + b'\n',
    "the other kind's digest": lambda repo, kind: _real_digest(repo, _other_kind(kind)),
    'the right digest without its newline': lambda repo, kind: _real_digest(repo, kind).rstrip(b'\n'),
    'the right digest twice': lambda repo, kind: _real_digest(repo, kind) * 2,
    'the right digest upper-cased': lambda repo, kind: _real_digest(repo, kind).upper(),
    'the right digest with a CRLF': lambda repo, kind: _real_digest(repo, kind).replace(b'\n', b'\r\n'),
    'empty': lambda repo, kind: b'',
}


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('content', list(_WRONG_CONTENT.values()), ids=list(_WRONG_CONTENT))
def test_a_stamp_that_is_not_byte_for_byte_the_current_digest_refuses(tmp_path: Path, kind: str, content):
    repo = _repo(tmp_path)
    _stamp(repo, kind).write_bytes(content(repo, kind))
    assert _refusal(repo, _trusted(tmp_path)) == NOT_CURRENT.format(tree=f'gen/proto/{kind}')


def test_the_two_kinds_have_different_digests():
    """Non-vacuity for "the other kind's digest": the case above would pass vacuously were they equal."""
    assert _real_digest(REPO_ROOT, 'python') != _real_digest(REPO_ROOT, 'ts')


# ---------------------------------------------------------------------------------------------------
# THE BAKED SCRIPT FAILING OR PRINTING ANYTHING BUT ONE DIGEST LINE


_BAD_OUTPUT = {
    'nothing': (b'', b'', 0),
    'not hex': (b'zz' * 32 + b'\n', b'', 0),
    'too short': (b'a' * 63 + b'\n', b'', 0),
    'no newline': (b'a' * 64, b'', 0),
    'two lines': (b'a' * 64 + b'\n' + b'a' * 64 + b'\n', b'', 0),
    'upper-case hex': (b'A' * 64 + b'\n', b'', 0),
    'a leading space': (b' ' + b'a' * 64 + b'\n', b'', 0),
}


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize(('stdout', 'stderr', 'status'), list(_BAD_OUTPUT.values()), ids=list(_BAD_OUTPUT))
def test_malformed_script_output_refuses_even_when_the_stamp_holds_it(
    tmp_path: Path, kind: str, stdout: bytes, stderr: bytes, status: int
):
    """A malformed digest is refused as such, not compared: a stamp holding the same bytes does not pass."""
    repo = _repo(tmp_path)
    _stamp(repo, kind).write_bytes(stdout)
    script = _trusted(tmp_path, _stub(kind, stdout, stderr, status))
    assert _refusal(repo, script) == f'the {kind} proto digest could not be computed (bad output): {HINT}'


@pytest.mark.parametrize('kind', KINDS)
@pytest.mark.parametrize('stdout', [b'', None], ids=['no stdout', 'a valid digest on stdout'])
def test_a_failing_script_refuses_with_the_first_line_of_its_reason(tmp_path: Path, kind: str, stdout):
    """A non-zero exit refuses even when stdout holds the stamp's own digest (the status is read)."""
    repo = _repo(tmp_path)
    printed = _stamp(repo, kind).read_bytes() if stdout is None else stdout
    script = _trusted(tmp_path, _stub(kind, printed, b'proto_digest.sh: it went wrong\nsecond line\n', 4))
    assert (
        _refusal(repo, script)
        == f'the {kind} proto digest could not be computed (proto_digest.sh: it went wrong): {HINT}'
    )


@pytest.mark.parametrize('error', [subprocess.TimeoutExpired(['bash'], 60), FileNotFoundError(2, 'no bash')])
def test_a_script_that_cannot_run_or_times_out_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error):
    repo = _repo(tmp_path)

    def run(*args, **kwargs):
        raise error

    monkeypatch.setattr(stack.subprocess, 'run', run)
    expected = f'the python proto digest could not be computed ({type(error).__name__}): {HINT}'
    assert _refusal(repo, _trusted(tmp_path)) == expected


def test_a_digest_the_real_script_refuses_refuses_the_guard(tmp_path: Path):
    """End to end with the real trusted script: a symlink under proto/ is its refusal, and so the guard's."""
    repo = _repo(tmp_path)
    (repo / 'proto' / 'linked.proto').symlink_to(repo / A_PROTO)
    message = _refusal(repo, _trusted(tmp_path))
    assert message.startswith('the python proto digest could not be computed (proto_digest.sh: refusing to digest'), (
        message
    )
    assert message.endswith(f'): {HINT}'), message


# ---------------------------------------------------------------------------------------------------
# CONTENT, NOT TIME


def test_identical_content_under_old_generated_mtimes_passes(tmp_path: Path):
    """The original bug: trees older than every .proto, generated from the same content, were refused."""
    repo = _repo(tmp_path)
    _set_mtimes(repo / 'gen', time.time() - 30 * 86400)
    _set_mtimes(repo / 'proto', time.time())
    _check(repo, _trusted(tmp_path))


def test_touching_a_proto_or_a_generated_file_or_a_stamp_changes_nothing(tmp_path: Path):
    repo = _repo(tmp_path)
    script = _trusted(tmp_path)
    for path in (repo / A_PROTO, repo / 'gen/proto/ts/trader_joe/proto/a/v1/a_pb.ts', _stamp(repo, 'python')):
        for when in (time.time() + 86400, time.time() - 86400, 0):
            os.utime(path, (when, when))
            _check(repo, script)


def test_an_edit_that_keeps_every_mtime_still_refuses(tmp_path: Path):
    """What mtime could never see: the content moved, the timestamps did not."""
    repo = _repo(tmp_path)
    proto = repo / A_PROTO
    before = os.stat(proto)
    proto.write_text('syntax = "proto3";\nmessage Edited {}\n')
    os.utime(proto, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert _refusal(repo, _trusted(tmp_path)) == NOT_CURRENT.format(tree='gen/proto/python')


def test_an_older_proto_with_an_unequal_stamp_refuses(tmp_path: Path):
    """The bead's other half: a .proto OLDER than every generated file is still refused when its content moved."""
    repo = _repo(tmp_path)
    (repo / A_PROTO).write_text('syntax = "proto3";\nmessage Older {}\n')
    os.utime(repo / A_PROTO, (0, 0))
    _set_mtimes(repo / 'gen', time.time() + 86400)
    assert _refusal(repo, _trusted(tmp_path)) == NOT_CURRENT.format(tree='gen/proto/python')


def _added(repo: Path) -> None:
    (repo / 'proto/trader_joe/proto/c/v1').mkdir(parents=True)
    (repo / 'proto/trader_joe/proto/c/v1/c.proto').write_text('syntax = "proto3";\n')


def _removed(repo: Path) -> None:
    (repo / 'proto/trader_joe/proto/b/v1/b.proto').unlink()


def _renamed(repo: Path) -> None:
    (repo / A_PROTO).rename(repo / 'proto/trader_joe/proto/a/v1/renamed.proto')


def _edited(repo: Path) -> None:
    (repo / A_PROTO).write_bytes((repo / A_PROTO).read_bytes() + b'// one more line\n')


@pytest.mark.parametrize('change', [_added, _removed, _renamed, _edited], ids=['add', 'remove', 'rename', 'edit'])
def test_any_proto_change_refuses_both_kinds(tmp_path: Path, change):
    repo = _repo(tmp_path)
    script = _trusted(tmp_path)
    change(repo)
    for tree in TREES:
        assert _refusal(repo, script, (tree,)) == NOT_CURRENT.format(tree=tree)


@pytest.mark.parametrize('config', ['web/buf.gen.yaml', 'buf.yaml'])
def test_a_ts_input_change_refuses_ts_and_not_python(tmp_path: Path, config: str):
    repo = _repo(tmp_path)
    script = _trusted(tmp_path)
    (repo / config).write_bytes((repo / config).read_bytes() + b'# changed\n')
    _check(repo, script, ('gen/proto/python',))
    assert _refusal(repo, script, ('gen/proto/ts',)) == NOT_CURRENT.format(tree='gen/proto/ts')


def test_regenerating_the_stamp_after_a_change_passes_again(tmp_path: Path):
    """The hint's fix works: after the change, a make-style rewrite of both stamps clears the refusal."""
    repo = _repo(tmp_path)
    script = _trusted(tmp_path)
    _edited(repo)
    assert _refusal(repo, script)
    write_generation_stamps(repo, repo / 'tools' / 'proto_digest.sh')
    _check(repo, script)


def test_the_guard_does_not_read_mtimes():
    """Source pin, alongside the behaviour above: no stat time and no mtime helper is left in the guard."""
    source = inspect.getsource(stack.check_generated_code_fresh) + inspect.getsource(stack._current_proto_digest)
    for word in ('mtime', 'st_mtime', 'getmtime', 'ctime'):
        assert word not in source, word
    assert not hasattr(stack, '_newest_mtime') and not hasattr(stack, 'DEV_PROTO_SOURCE_DIR')


# ---------------------------------------------------------------------------------------------------
# ORDER AND HINTS


def test_a_missing_tree_is_reported_before_its_missing_stamp(tmp_path: Path):
    repo = _repo(tmp_path)
    shutil.rmtree(repo / 'gen/proto/ts')
    _stamp(repo, 'ts').unlink()
    assert _refusal(repo, _trusted(tmp_path)) == MISSING.format(tree='gen/proto/ts')


@pytest.mark.parametrize('kind', KINDS)
def test_a_missing_stamp_refuses_before_the_script_runs_for_that_kind(tmp_path: Path, kind: str):
    """The stamp is read first: a script that would fail for KIND is not reached, and so not run for it."""
    repo = _repo(tmp_path)
    _stamp(repo, kind).unlink()
    script = _trusted(tmp_path, _stub(kind, b'', b'must not run\n', 9))
    if kind == 'python':
        assert _refusal(repo, script) == NO_STAMP.format(tree='gen/proto/python')
        assert not (script.parent / 'ran').exists(), 'the script ran although the stamp was missing'
    else:
        # python passes first, through the stub's real branch; ts then stops at its stamp.
        assert _refusal(repo, script) == NO_STAMP.format(tree='gen/proto/ts')


def test_the_trees_are_judged_in_order_python_first(tmp_path: Path):
    repo = _repo(tmp_path)
    _stamp(repo, 'python').write_bytes(b'0' * 64 + b'\n')
    shutil.rmtree(repo / 'gen/proto/ts')
    assert _refusal(repo, _trusted(tmp_path)) == NOT_CURRENT.format(tree='gen/proto/python')


def test_every_refusal_ends_with_the_hint_it_was_given(tmp_path: Path):
    repo = _repo(tmp_path)
    script = _trusted(tmp_path)
    _stamp(repo, 'python').unlink()
    message = _refusal(repo, script, ('gen/proto/python',), TEST_HINT)
    assert (
        message
        == f'no generation stamp: gen/proto/python.digest is missing, unreadable or not a regular file: {TEST_HINT}'
    )
    assert stack._GENERATE_HINT == HINT and stack.TEST_GENERATE_HINT == TEST_HINT
    defaults = inspect.signature(stack.check_generated_code_fresh).parameters
    assert defaults['hint'].default == HINT and defaults['generated_dirs'].default == TREES


# ---------------------------------------------------------------------------------------------------
# THE BAKED COPY, NEVER THE CHECKOUT'S


def test_the_guard_runs_the_baked_script_by_default():
    defaults = inspect.signature(stack.check_generated_code_fresh).parameters
    assert str(defaults['trusted_script'].default) == TRUSTED_SCRIPT
    assert str(stack.TRUSTED_PROTO_DIGEST) == TRUSTED_SCRIPT
    assert stack.proto_digest_argv(Path('/r'), 'ts') == ('/usr/bin/bash', TRUSTED_SCRIPT, '--context', '/r', 'ts')


def test_the_guard_runs_exactly_two_fixed_argv_lists_and_no_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Recorded with the default script: bash, the baked path, --context, the repository root, the kind."""
    repo = _repo(tmp_path)
    calls = []

    def run(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        kind = argv[-1]
        return subprocess.CompletedProcess(argv, 0, _stamp(repo, kind).read_bytes(), b'')

    monkeypatch.setattr(stack.subprocess, 'run', run)
    stack.check_generated_code_fresh(repo)
    assert [argv for argv, _ in calls] == [
        ('/usr/bin/bash', TRUSTED_SCRIPT, '--context', str(repo), kind) for kind in KINDS
    ]
    for _, kwargs in calls:
        assert not kwargs.get('shell') and kwargs['env'] == {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}, kwargs
        assert kwargs['cwd'] == '/' and kwargs['timeout'] and kwargs['capture_output'], kwargs


def test_the_checkouts_own_script_is_never_run_and_cannot_vouch_for_a_stamp(tmp_path: Path):
    """An agent rewrites the checkout's tools/proto_digest.sh to print what it planted in the stamps."""
    repo = _repo(tmp_path)
    planted = b'f' * 64 + b'\n'
    own = repo / 'tools' / 'proto_digest.sh'
    own.write_text(f'#! /bin/bash\n: > "{tmp_path}/checkout-script-ran"\nprintf %s {shlex.quote(planted.decode())}\n')
    for kind in KINDS:
        _stamp(repo, kind).write_bytes(planted)
    assert _refusal(repo, _trusted(tmp_path)) == NOT_CURRENT.format(tree='gen/proto/python')
    assert not (tmp_path / 'checkout-script-ran').exists()


@pytest.mark.parametrize('verb', ['build', 'up', 'test_build'])
def test_the_verbs_call_the_guard_with_the_baked_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    """No verb passes trusted_script, so each one runs the baked copy (the rig alone substitutes a tmp one)."""
    rig = make_rig(tmp_path, monkeypatch, FakeDocker())
    seen = []

    def record(*args, **kwargs):
        seen.append(kwargs)
        raise stack.Refused('recorded')

    monkeypatch.setattr(stack, 'check_generated_code_fresh', record)
    result = rig.call(verb, {})
    assert seen == [{}], (verb, seen, result)


# ---------------------------------------------------------------------------------------------------
# tools/proto_digest.sh --context <dir>


def _script_alone(tmp_path: Path) -> Path:
    """A copy of the script with no proto/ beside it, as in the MCP image."""
    script = tmp_path / 'baked' / 'tools' / 'proto_digest.sh'
    script.parent.mkdir(parents=True)
    shutil.copy2(REAL_PROTO_DIGEST, script)
    return script


def _run(script: Path, *arguments: str, cwd: Path | str = '/') -> subprocess.CompletedProcess:
    return subprocess.run(  # nosec B603 -- argument list, no shell
        ['/usr/bin/bash', str(script), *arguments], cwd=cwd, capture_output=True, check=False, timeout=60
    )


@pytest.mark.parametrize('kind', KINDS)
def test_context_digests_the_other_checkout_exactly_as_its_own_copy_does(tmp_path: Path, kind: str):
    repo = _tree(tmp_path)
    in_tree = _run(repo / 'tools' / 'proto_digest.sh', kind)
    assert in_tree.returncode == 0, in_tree.stderr
    script = _script_alone(tmp_path)
    for cwd in ('/', tmp_path, repo / 'proto'):
        baked = _run(script, '--context', str(repo), kind, cwd=cwd)
        assert baked.returncode == 0 and baked.stderr == b'', baked.stderr
        assert baked.stdout == in_tree.stdout == _expected(repo, kind).encode() + b'\n'


@pytest.mark.parametrize('kind', KINDS)
def test_context_over_the_real_checkout_matches_the_in_tree_run(tmp_path: Path, kind: str):
    in_tree = _run(REAL_PROTO_DIGEST, kind)
    baked = _run(_script_alone(tmp_path), '--context', str(REPO_ROOT), kind)
    assert in_tree.returncode == baked.returncode == 0, (in_tree.stderr, baked.stderr)
    assert baked.stdout == in_tree.stdout


def test_without_context_the_baked_copy_refuses_rather_than_digesting_its_own_directory(tmp_path: Path):
    result = _run(_script_alone(tmp_path), 'python')
    assert result.returncode == 4 and result.stdout == b'', result
    assert b'proto/ is missing' in result.stderr


_BAD_ARGUMENTS = {
    'flag with no value': ['--context'],
    'flag and dir, no target': ['--context', '{repo}'],
    'a missing directory': ['--context', '{repo}/nowhere', 'python'],
    'a file, not a directory': ['--context', '{repo}/buf.yaml', 'python'],
    'an empty value': ['--context', '', 'python'],
    'flag after the target': ['python', '--context', '{repo}'],
    'the flag twice': ['--context', '{repo}', '--context', '{repo}', 'python'],
    'an unknown target': ['--context', '{repo}', 'go'],
    'a look-alike flag': ['--context={repo}', 'python'],
}


@pytest.mark.parametrize('arguments', list(_BAD_ARGUMENTS.values()), ids=list(_BAD_ARGUMENTS))
def test_a_malformed_context_invocation_is_a_usage_error_with_nothing_on_stdout(tmp_path: Path, arguments):
    repo = _tree(tmp_path)
    result = _run(_script_alone(tmp_path), *(word.format(repo=repo) for word in arguments))
    assert result.returncode == 2, (result.returncode, result.stderr)
    assert result.stdout == b'' and result.stderr.strip(), result


def _proto_a_link(repo: Path, tmp_path: Path) -> None:
    shutil.move(repo / 'proto', tmp_path / 'real-proto')
    (repo / 'proto').symlink_to(tmp_path / 'real-proto', target_is_directory=True)


def _a_link_under_proto(repo: Path, tmp_path: Path) -> None:
    (repo / 'proto' / 'x.proto').symlink_to(repo / A_PROTO)


def _buf_yaml_a_link(repo: Path, tmp_path: Path) -> None:
    (tmp_path / 'buf.yaml').write_bytes((repo / 'buf.yaml').read_bytes())
    (repo / 'buf.yaml').unlink()
    (repo / 'buf.yaml').symlink_to(tmp_path / 'buf.yaml')


def _web_a_link(repo: Path, tmp_path: Path) -> None:
    shutil.move(repo / 'web', tmp_path / 'real-web')
    (repo / 'web').symlink_to(tmp_path / 'real-web', target_is_directory=True)


@pytest.mark.parametrize(
    ('make_bad', 'kinds'),
    [(_proto_a_link, KINDS), (_a_link_under_proto, KINDS), (_buf_yaml_a_link, ('ts',)), (_web_a_link, ('ts',))],
    ids=['proto/ a link', 'a link under proto/', 'buf.yaml a link', 'web/ a link'],
)
def test_every_refusal_still_applies_under_context(tmp_path: Path, make_bad, kinds):
    """--context points the TRUSTED script at a tree; the tree's links are refused there as in place."""
    repo = _tree(tmp_path)
    make_bad(repo, tmp_path)
    for kind in kinds:
        result = _run(_script_alone(tmp_path / kind), '--context', str(repo), kind)
        assert result.returncode == 4 and result.stdout == b'', (kind, result)
