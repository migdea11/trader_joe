"""`npm run gen:proto` stamps every TypeScript generation: web/scripts/gen-proto.sh (bead tj-grna9p.186).

The web pre-scripts (predev, prebuild, pretypecheck, pretest) and `make gen-proto-ts` all regenerate
gen/proto/ts through `npm run gen:proto`, which runs web/scripts/gen-proto.sh. The script deletes
gen/proto/ts.digest FIRST, runs `buf generate ../proto --template buf.gen.yaml` from web/, takes the
digest from tools/proto_digest.sh ts, writes it to a temporary file beside the stamp and renames it into
place LAST. A failed generation, a failed or empty digest, output that is not exactly 64 lowercase hex
characters, a failed rename or an interrupted run leaves no stamp, exits non-zero (128+signal for HUP,
INT and TERM) and leaves no temporary file behind. An inherited CDPATH does not move the run off web/.

The real script runs, from a copy in a scratch repository, with the real tools/proto_digest.sh beside it
(the digest is compared with the documented manifest of test_proto_digest_make). buf, and where a case
needs it mv, are stood in on PATH: each stand-in records whether a stamp, or a temporary stamp, existed
at the moment it ran, which is what proves "delete first, write last". Real npm runs the copied
package.json, from web/ and through `--prefix web` from the repository root.

NOT RUN here: real buf or protoc-gen-es (the generated tree's content is buf's), and a SIGKILL of the
script itself between the temporary write and the rename: that leaves a `ts.digest.tmp.<pid>` file and
no stamp (no trap runs on SIGKILL), which fails closed for the stamp and is not tested.
"""

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from common.tests.test_ci_invariants import REPO_ROOT, _subprocess_env
from common.tests.test_make_lint import _executable
from common.tests.test_proto_digest_make import TS_STAMP, _expected, _tree


pytestmark = pytest.mark.build_infra

GEN_SCRIPT = REPO_ROOT / 'web' / 'scripts' / 'gen-proto.sh'
PACKAGE_JSON = REPO_ROOT / 'web' / 'package.json'
TIMEOUT_S = 60
STALE = 'stale-stamp-from-an-earlier-generation\n'
PRE_HOOKS = ('predev', 'prebuild', 'pretypecheck', 'pretest')

# Records the stamp's state while buf "runs", its working directory and arguments, then regenerates
# ../gen/proto/ts as clean: true would (remove, then write) and exits BUF_EXIT. BUF_SLEEP holds it open
# so a test can signal the script mid-generation.
BUF_PROBE = r"""#!/bin/sh
stamp="$PROBE_ROOT/gen/proto/ts.digest"
if [ -e "$stamp" ]; then s=present; else s=absent; fi
tmps=$(ls "$PROBE_ROOT/gen/proto" 2>/dev/null | grep -c '^ts\.digest\.tmp' || true)
printf 'buf stamp=%s tmp=%s cwd=%s args=%s\n' "$s" "$tmps" "$(pwd)" "$*" >> "$PROBE_LOG"
rm -rf ../gen/proto/ts
mkdir -p ../gen/proto/ts
echo generated > ../gen/proto/ts/a_pb.ts
if [ -n "${BUF_SLEEP:-}" ]; then sleep "$BUF_SLEEP"; fi
exit "${BUF_EXIT:-0}"
"""

# Wraps the real digest script (copied beside it as proto_digest.real.sh): records the stamp's state,
# then digests, or misbehaves as DIGEST_MODE says.
DIGEST_PROBE = r"""#!/bin/bash
stamp="$PROBE_ROOT/gen/proto/ts.digest"
if [ -e "$stamp" ]; then s=present; else s=absent; fi
printf 'digest stamp=%s args=%s\n' "$s" "$*" >> "$PROBE_LOG"
case "${DIGEST_MODE:-}" in
    fail) echo "refused" >&2; exit 3 ;;
    empty) exit 0 ;;
    print-then-fail) bash "$(dirname "$0")/proto_digest.real.sh" "$@"; exit 5 ;;
    text) printf '%s' "$DIGEST_TEXT"; exit 0 ;;
esac
exec bash "$(dirname "$0")/proto_digest.real.sh" "$@"
"""

# Records the stamp's and the temporary file's state when the rename runs, then renames, or fails or
# dies as MV_MODE says.
MV_PROBE = r"""#!/bin/sh
stamp="$PROBE_ROOT/gen/proto/ts.digest"
if [ -e "$stamp" ]; then s=present; else s=absent; fi
tmps=$(ls "$PROBE_ROOT/gen/proto" 2>/dev/null | grep -c '^ts\.digest\.tmp' || true)
printf 'mv stamp=%s tmp=%s\n' "$s" "$tmps" >> "$PROBE_LOG"
case "${MV_MODE:-}" in
    fail) exit 1 ;;
    killed) kill -KILL $$ ;;
esac
exec "$REAL_MV" "$@"
"""


# ---------------------------------------------------------------------------------------------------
# HELPERS


def _repo(tmp_path: Path) -> Path:
    """The digest test's scratch repository, plus the real gen-proto.sh, package.json and a probe digest."""
    root = _tree(tmp_path)
    (root / 'web' / 'scripts').mkdir()
    shutil.copy2(GEN_SCRIPT, root / 'web' / 'scripts' / 'gen-proto.sh')
    shutil.copy2(PACKAGE_JSON, root / 'web' / 'package.json')
    tools = root / 'tools'
    (tools / 'proto_digest.sh').rename(tools / 'proto_digest.real.sh')
    _executable(tools / 'proto_digest.sh', DIGEST_PROBE)
    return root


def _env(tmp_path: Path, root: Path, *, mv: bool = False, **values: str) -> dict[str, str]:
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir(exist_ok=True)
    _executable(bin_dir / 'buf', BUF_PROBE)
    real_mv = shutil.which('mv')
    assert real_mv, 'no mv on PATH'
    if mv:
        _executable(bin_dir / 'mv', MV_PROBE)
    path = f'{bin_dir}{os.pathsep}{os.environ["PATH"]}'
    assert shutil.which('buf', path=path) == str(bin_dir / 'buf'), 'the buf stand-in does not shadow buf'
    return _subprocess_env(
        PATH=path,
        PROBE_ROOT=str(root),
        PROBE_LOG=str(tmp_path / 'probe.log'),
        REAL_MV=real_mv,
        npm_config_cache=str(tmp_path / 'npm-cache'),
        npm_config_update_notifier='false',
        npm_config_fund='false',
        npm_config_audit='false',
        **values,
    )


def _gen(tmp_path: Path, root: Path, *, mv: bool = False, **values: str) -> subprocess.CompletedProcess:
    """Run the script as npm does: `sh scripts/gen-proto.sh` from web/."""
    return subprocess.run(
        ['sh', 'scripts/gen-proto.sh'],
        cwd=root / 'web',
        capture_output=True,
        text=True,
        env=_env(tmp_path, root, mv=mv, **values),
        timeout=TIMEOUT_S,
        check=False,
    )


def _probe(tmp_path: Path) -> list[str]:
    log = tmp_path / 'probe.log'
    return log.read_text().splitlines() if log.exists() else []


def _kinds(tmp_path: Path) -> list[str]:
    return [line.split(' ', 1)[0] for line in _probe(tmp_path)]


def _seed(root: Path) -> None:
    stamp = root / TS_STAMP
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(STALE)


def _stray(root: Path) -> list[str]:
    """Anything in gen/proto beside the generated tree and the stamp: a temporary file left behind."""
    gen = root / 'gen' / 'proto'
    if not gen.exists():
        return []
    return sorted(path.name for path in gen.iterdir() if path.name not in {'ts', 'ts.digest'})


def _no_stamp(root: Path, result: subprocess.CompletedProcess) -> None:
    assert result.returncode != 0, f'the script succeeded: {result.stdout!r} {result.stderr!r}'
    assert not (root / TS_STAMP).exists(), 'a failed run left a stamp'
    assert _stray(root) == [], f'a failed run left temporary files: {_stray(root)}'


# ---------------------------------------------------------------------------------------------------
# SUCCESS: delete first, generate, digest, write last


def test_the_stamp_is_the_digest_of_the_inputs(tmp_path: Path):
    root = _repo(tmp_path)
    result = _gen(tmp_path, root)
    assert result.returncode == 0, result.stderr
    assert (root / TS_STAMP).read_text() == _expected(root, 'ts') + '\n'
    assert (root / 'gen' / 'proto' / 'ts' / 'a_pb.ts').exists(), 'buf did not run'


def test_the_old_stamp_is_deleted_before_buf_and_the_new_one_written_last(tmp_path: Path):
    root = _repo(tmp_path)
    _seed(root)
    result = _gen(tmp_path, root, mv=True)
    assert result.returncode == 0, result.stderr
    lines = _probe(tmp_path)
    assert _kinds(tmp_path) == ['buf', 'digest', 'mv'], lines
    assert lines[0].startswith('buf stamp=absent tmp=0 '), f'a stamp existed while buf ran: {lines[0]}'
    assert lines[1] == 'digest stamp=absent args=ts', lines[1]
    assert lines[2] == 'mv stamp=absent tmp=1', f'the stamp was not renamed into place from a temp: {lines[2]}'
    assert (root / TS_STAMP).read_text() == _expected(root, 'ts') + '\n'


def test_buf_runs_from_web_with_the_unchanged_arguments(tmp_path: Path):
    root = _repo(tmp_path)
    assert _gen(tmp_path, root).returncode == 0
    (line,) = [entry for entry in _probe(tmp_path) if entry.startswith('buf ')]
    assert line.endswith(f' cwd={root / "web"} args=generate ../proto --template buf.gen.yaml'), line


def test_no_temporary_file_remains_after_success(tmp_path: Path):
    root = _repo(tmp_path)
    assert _gen(tmp_path, root).returncode == 0
    assert _stray(root) == []


def test_running_twice_gives_the_identical_stamp(tmp_path: Path):
    root = _repo(tmp_path)
    assert _gen(tmp_path, root).returncode == 0
    first = (root / TS_STAMP).read_bytes()
    assert _gen(tmp_path, root).returncode == 0
    assert (root / TS_STAMP).read_bytes() == first
    assert _stray(root) == []


def test_an_input_change_moves_the_stamp(tmp_path: Path):
    root = _repo(tmp_path)
    assert _gen(tmp_path, root).returncode == 0
    first = (root / TS_STAMP).read_text()
    proto = next((root / 'proto').rglob('*.proto'))
    proto.write_bytes(proto.read_bytes() + b'// edited\n')
    assert _gen(tmp_path, root).returncode == 0
    assert (root / TS_STAMP).read_text() != first
    assert (root / TS_STAMP).read_text() == _expected(root, 'ts') + '\n'


@pytest.mark.parametrize('form', ['from-root', 'absolute-from-elsewhere'])
def test_the_script_runs_from_web_whatever_the_callers_directory(tmp_path: Path, form: str):
    root = _repo(tmp_path)
    elsewhere = tmp_path / 'elsewhere'
    elsewhere.mkdir()
    script, cwd = {
        'from-root': ('web/scripts/gen-proto.sh', root),
        'absolute-from-elsewhere': (str(root / 'web' / 'scripts' / 'gen-proto.sh'), elsewhere),
    }[form]
    result = subprocess.run(
        ['sh', script], cwd=cwd, capture_output=True, text=True,
        env=_env(tmp_path, root), timeout=TIMEOUT_S, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert f'cwd={root / "web"} ' in _probe(tmp_path)[0]
    assert (root / TS_STAMP).read_text() == _expected(root, 'ts') + '\n'
    assert not (elsewhere / 'gen').exists() and not (root / 'web' / 'gen').exists()


# ---------------------------------------------------------------------------------------------------
# FAILURE: no stamp, non-zero, no temporary file


def test_a_failing_buf_leaves_no_stamp_and_removes_the_old_one(tmp_path: Path):
    root = _repo(tmp_path)
    _seed(root)
    result = _gen(tmp_path, root, BUF_EXIT='1')
    _no_stamp(root, result)
    assert _kinds(tmp_path) == ['buf'], 'the script went on past a failed buf'


def test_a_failing_buf_on_a_fresh_tree_leaves_no_stamp(tmp_path: Path):
    root = _repo(tmp_path)
    _no_stamp(root, _gen(tmp_path, root, BUF_EXIT='2'))


@pytest.mark.parametrize('mode', ['fail', 'empty', 'print-then-fail'])
def test_a_failing_or_empty_digest_leaves_no_stamp(tmp_path: Path, mode: str):
    root = _repo(tmp_path)
    _seed(root)
    result = _gen(tmp_path, root, mv=True, DIGEST_MODE=mode)
    _no_stamp(root, result)
    assert _kinds(tmp_path) == ['buf', 'digest'], f'the script wrote on past a failed digest: {_probe(tmp_path)}'


def test_an_empty_digest_is_named_on_stderr(tmp_path: Path):
    root = _repo(tmp_path)
    result = _gen(tmp_path, root, DIGEST_MODE='empty')
    assert 'printed no digest' in result.stderr, result.stderr


GOOD_HEX = '0123456789abcdef' * 4
NOT_A_DIGEST = {
    'stray-line-after': GOOD_HEX + '\nwarning: something\n',
    'stray-line-before': 'note\n' + GOOD_HEX + '\n',
    'two-digests': GOOD_HEX + '\n' + GOOD_HEX + '\n',
    '63-hex': GOOD_HEX[:63] + '\n',
    '65-hex': GOOD_HEX + 'a\n',
    'upper-case': GOOD_HEX.upper() + '\n',
    'one-upper-case': 'A' + GOOD_HEX[1:] + '\n',
    'non-hex': 'g' * 64 + '\n',
    'leading-space': ' ' + GOOD_HEX[1:] + '\n',
    'inner-space': GOOD_HEX[:32] + ' ' + GOOD_HEX[33:] + '\n',
    'name-after-it': GOOD_HEX + '  -\n',
}


@pytest.mark.parametrize('locale', ['C', 'C.UTF-8'])
@pytest.mark.parametrize('text', NOT_A_DIGEST.values(), ids=NOT_A_DIGEST.keys())
def test_output_that_is_not_one_lowercase_sha256_leaves_no_stamp(tmp_path: Path, text: str, locale: str):
    """A stray line, a wrong length, upper case or a non-hex character fails closed, under any locale."""
    root = _repo(tmp_path)
    _seed(root)
    result = _gen(tmp_path, root, mv=True, DIGEST_MODE='text', DIGEST_TEXT=text, LC_ALL=locale)
    _no_stamp(root, result)
    assert 'proto_digest.sh' in result.stderr, f'no reason on stderr: {result.stderr!r}'
    assert _kinds(tmp_path) == ['buf', 'digest'], f'the script wrote on past a bad digest: {_probe(tmp_path)}'


@pytest.mark.parametrize('text', [GOOD_HEX, GOOD_HEX + '\n', 'f' * 64 + '\n'], ids=['no-newline', 'newline', 'all-f'])
def test_exactly_64_lowercase_hex_characters_are_stamped(tmp_path: Path, text: str):
    root = _repo(tmp_path)
    result = _gen(tmp_path, root, DIGEST_MODE='text', DIGEST_TEXT=text)
    assert result.returncode == 0, result.stderr
    assert (root / TS_STAMP).read_text() == text.strip() + '\n'
    assert _stray(root) == []


def test_a_cdpath_that_would_shadow_scripts_does_not_move_the_run(tmp_path: Path):
    """POSIX cd consults CDPATH for the relative `scripts/..` that npm's `sh scripts/gen-proto.sh` gives."""
    root = _repo(tmp_path)
    decoy = tmp_path / 'decoy'
    (decoy / 'scripts').mkdir(parents=True)
    (decoy / 'buf.gen.yaml').write_text('decoy\n')
    result = _gen(tmp_path, root, CDPATH=str(decoy))
    assert result.returncode == 0, result.stderr
    assert f'cwd={root / "web"} ' in _probe(tmp_path)[0], _probe(tmp_path)
    assert (root / TS_STAMP).read_text() == _expected(root, 'ts') + '\n'
    assert not (tmp_path / 'gen').exists(), 'the run escaped into the CDPATH decoy'
    assert result.stdout == '', f'cd printed a CDPATH hit: {result.stdout!r}'


@pytest.mark.parametrize('mode', ['fail', 'killed'])
def test_a_failed_or_killed_rename_leaves_no_stamp_and_no_temporary_file(tmp_path: Path, mode: str):
    root = _repo(tmp_path)
    _seed(root)
    result = _gen(tmp_path, root, mv=True, MV_MODE=mode)
    _no_stamp(root, result)
    assert _probe(tmp_path)[-1] == 'mv stamp=absent tmp=1'


def _interrupted(tmp_path: Path, root: Path, deliver) -> subprocess.CompletedProcess:
    """Start the script with buf held open, and once buf is running deliver a signal to it."""
    process = subprocess.Popen(
        ['sh', 'scripts/gen-proto.sh'], cwd=root / 'web', stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=_env(tmp_path, root, BUF_SLEEP='3'), start_new_session=True,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + TIMEOUT_S
        while not _probe(tmp_path):
            assert time.monotonic() < deadline, 'buf never started'
            time.sleep(0.05)
        deliver(process)
        stdout, stderr = process.communicate(timeout=TIMEOUT_S)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def test_an_interrupt_to_the_process_group_mid_generation_leaves_no_stamp(tmp_path: Path):
    """Ctrl-C: SIGINT reaches buf and the script together."""
    root = _repo(tmp_path)
    _seed(root)
    result = _interrupted(tmp_path, root, lambda process: os.killpg(process.pid, signal.SIGINT))
    _no_stamp(root, result)


@pytest.mark.parametrize('signum', [signal.SIGTERM, signal.SIGINT, signal.SIGHUP], ids=['term', 'int', 'hup'])
def test_a_signal_to_the_script_alone_mid_generation_stops_it_with_no_stamp(tmp_path: Path, signum: int):
    """npm, or a timeout, signals the shell it started, not buf: the script must stop, not carry on and stamp."""
    root = _repo(tmp_path)
    _seed(root)
    result = _interrupted(tmp_path, root, lambda process: process.send_signal(signum))
    _no_stamp(root, result)
    assert _kinds(tmp_path) == ['buf'], f'the script went on past {signum!r}: {_probe(tmp_path)}'
    assert result.returncode == 128 + signum, f'{signum!r} did not exit 128+signal: {result.returncode}'


# ---------------------------------------------------------------------------------------------------
# THE WIRING: package.json, through real npm


def _scripts() -> dict[str, str]:
    return json.loads(PACKAGE_JSON.read_text(encoding='utf-8'))['scripts']


def test_gen_proto_runs_the_script_and_every_pre_hook_runs_gen_proto():
    scripts = _scripts()
    assert scripts['gen:proto'] == 'sh scripts/gen-proto.sh'
    for hook in PRE_HOOKS:
        assert scripts[hook] == 'npm run gen:proto', hook
    assert GEN_SCRIPT.is_file()


def test_no_npm_script_runs_buf_or_the_digest_directly():
    """The script is the one TypeScript generator: a direct `buf generate` in an npm script would not stamp."""
    for name, command in _scripts().items():
        assert 'buf' not in command and 'digest' not in command, f'{name}: {command!r}'


@pytest.mark.skipif(shutil.which('npm') is None, reason='npm is not installed')
@pytest.mark.parametrize('form', ['from-web', 'prefix-from-root'])
def test_npm_run_gen_proto_stamps(tmp_path: Path, form: str):
    root = _repo(tmp_path)
    _seed(root)
    command, cwd = {
        'from-web': (['npm', 'run', 'gen:proto'], root / 'web'),
        'prefix-from-root': (['npm', '--prefix', 'web', 'run', 'gen:proto'], root),
    }[form]
    result = subprocess.run(
        command, cwd=cwd, capture_output=True, text=True,
        env=_env(tmp_path, root), timeout=TIMEOUT_S * 2, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert _probe(tmp_path)[0].startswith('buf stamp=absent tmp=0 '), _probe(tmp_path)
    assert (root / TS_STAMP).read_text() == _expected(root, 'ts') + '\n'
    assert _stray(root) == []


@pytest.mark.skipif(shutil.which('npm') is None, reason='npm is not installed')
def test_a_failing_npm_run_gen_proto_fails_and_leaves_no_stamp(tmp_path: Path):
    root = _repo(tmp_path)
    _seed(root)
    result = subprocess.run(
        ['npm', 'run', 'gen:proto'], cwd=root / 'web', capture_output=True, text=True,
        env=_env(tmp_path, root, BUF_EXIT='1'), timeout=TIMEOUT_S * 2, check=False,
    )  # fmt: skip
    _no_stamp(root, result)
