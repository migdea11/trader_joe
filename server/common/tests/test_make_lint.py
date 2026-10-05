"""`make lint`'s proto leg and `make buf-install`, run for real on scratch trees (tj-3mk3u5.54 gate 1-6 and 9).

The user's ruling (tj-3mk3u5.55): `make lint` is the ONE multi-language lint entry point. Python is ruff,
as it always was; proto is buf -- lint, format check, and breaking against main, report-only until the
first SDK release (tj-d2mhru). Generation stays protoc (`make proto`; test_make_proto.py).

Two kinds of test, as the bead's gate asks for:

* THE MAKEFILE'S LOGIC, with a stand-in buf on PATH that records every call and exits as the test says,
  run from a scratch git repository through the repository's own Makefile (-f), so the PATHS selector,
  the pin check, the order of the checks, the report-only downgrade and the git-archive baseline are
  each shown by what the recipe does rather than by what its text says. Every one of these runs filters
  the real buf off PATH first, so "no buf" means no buf wherever the pinned one is installed
  (~/.local/bin, the image's or CI's /usr/local/bin).
* THE CONFIGURATION'S SEMANTICS, with the REAL pinned buf and the repository's buf.yaml copied byte for
  byte: what STANDARD rejects, that a comment cannot exempt a rule, that FILE reports a renumbered field,
  and that internal/ -- and only internal/ -- is ignored. Every "not reported" case has a control that IS
  reported, so none of them can pass by looking at nothing. These FAIL when the pinned buf is absent,
  never skip (pytest.ini): `make buf-install` installs it until the agent image carries it.

`make buf-install` runs against a fake release served over file://, with HOME and BUF_INSTALL_DIR both
inside the test's temporary directory, so no run can reach a real buf.

Not provable here, and left to CI and the user's batched rebuild: the root install into /usr/local/bin,
annotations rendering on a pull request, and the agent-image build itself.
"""

import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from common.tests.test_ci_invariants import (
    MAKEFILE,
    REPO_ROOT,
    _expanded_make_variable,
    _fetched_refs,
    _lint_job_steps,
    _make_lint_command,
    _only_step,
)


pytestmark = pytest.mark.build_infra

BUF_YAML = REPO_ROOT / 'buf.yaml'
MAKE_TIMEOUT_S = 120
SEPARATOR = '\x1f'

# What the Makefile reads with ?=, or what an outer make carries inward: under `make test PATHS=common/tests`
# MAKEFLAGS holds PATHS=common/tests, and every make started here would silently inherit it.
_CALLER_VARIABLES = frozenset({'MAKEFLAGS', 'MFLAGS', 'MAKELEVEL', 'PATHS', 'PYTEST_ADDOPTS'})
_CALLER_PREFIXES = ('BUF', 'GIT_', 'UNAME_', 'CURL_')
# Scratch repositories only: the caller's git configuration cannot reach them.
GIT_ISOLATION = {
    'GIT_CONFIG_NOSYSTEM': '1',
    'GIT_CONFIG_GLOBAL': os.devnull,
    'GIT_AUTHOR_NAME': 'validator',
    'GIT_AUTHOR_EMAIL': 'validator@example.invalid',
    'GIT_COMMITTER_NAME': 'validator',
    'GIT_COMMITTER_EMAIL': 'validator@example.invalid',
}
# Everything the lint-proto, lint-fix-proto and buf-install recipes run, besides buf itself.
RECIPE_TOOLS = ('git', 'tar', 'mktemp', 'rm', 'cp', 'mkdir', 'mv', 'chmod', 'curl', 'sha256sum', 'cut', 'uname')

NEW_TARGETS = ('lint', 'lint-python', 'lint-proto', 'lint-fix', 'lint-fix-python', 'lint-fix-proto', 'buf-install')

# A stand-in buf. Each call is one line of the log, its arguments separated by US, so a word split or
# glued by the shell shows as a different list. --version answers BUF_STUB_VERSION; each check exits as
# its variable says. `breaking` copies the --against directory aside, so the test can read exactly the
# baseline buf was handed, after the recipe's trap has removed it.
BUF_STUB = r"""#!/bin/sh
for word in "$@"; do printf '%s\037' "$word"; done >> "$BUF_STUB_LOG"
printf '\n' >> "$BUF_STUB_LOG"
case "$1" in
  --version)
    printf '%s\n' "$BUF_STUB_VERSION"
    exit "${BUF_STUB_VERSION_EXIT:-0}" ;;
  lint) exit "${BUF_STUB_LINT_EXIT:-0}" ;;
  format) exit "${BUF_STUB_FORMAT_EXIT:-0}" ;;
  build)
    if [ "$#" -gt 1 ]; then exit "${BUF_STUB_BASELINE_BUILD_EXIT:-0}"; fi
    exit "${BUF_STUB_BUILD_EXIT:-0}" ;;
  breaking)
    while [ "$#" -gt 0 ]; do
      if [ "$1" = --against ]; then cp -R "$2" "$BUF_STUB_BASELINE_COPY"; fi
      shift
    done
    if [ "${BUF_STUB_BREAKING_EXIT:-0}" = 100 ]; then echo 'stub finding: field "1" on ProbeRequest was deleted'; fi
    exit "${BUF_STUB_BREAKING_EXIT:-0}" ;;
esac
echo "buf stub: unexpected call: $*" >&2
exit 97
"""
UV_STUB = r"""#!/bin/sh
for word in "$@"; do printf '%s\037' "$word"; done >> "$UV_STUB_LOG"
printf '\n' >> "$UV_STUB_LOG"
"""
UNAME_STUB = r"""#!/bin/sh
case "$*" in
  -s) printf '%s\n' "$UNAME_S" ;;
  -m) printf '%s\n' "$UNAME_M" ;;
  *) echo "uname stub: unexpected arguments: $*" >&2; exit 97 ;;
esac
"""
# The real curl, behind a wrapper that records its arguments: the file:// release is fetched for real.
CURL_WRAPPER = """#!/bin/sh
for word in "$@"; do printf '%s\\037' "$word"; done >> "$CURL_LOG"
printf '\\n' >> "$CURL_LOG"
exec {curl} "$@"
"""

EXTERNAL_PROTO = 'proto/trader_joe/proto/probe/v1/probe.proto'
INTERNAL_PROTO = 'proto/trader_joe/proto/internal/probe/v1/probe.proto'
EXTERNAL_PACKAGE = 'trader_joe.proto.probe.v1'
INTERNAL_PACKAGE = 'trader_joe.proto.internal.probe.v1'
# Appended to a contract: STANDARD's ENUM_VALUE_PREFIX wants BUY spelled SIDE_BUY.
UNPREFIXED_ENUM = '\nenum Side {\n  SIDE_UNSPECIFIED = 0;\n  BUY = 1;\n}\n'
IGNORED_UNPREFIXED_ENUM = (
    '\nenum Side {\n  SIDE_UNSPECIFIED = 0;\n  // buf:lint:ignore ENUM_VALUE_PREFIX\n  BUY = 1;\n}\n'
)


def _contract(package: str, request_field: int = 1) -> str:
    """A STANDARD-clean contract, in buf format, whose request field number the caller chooses."""
    return (
        'syntax = "proto3";\n'
        '\n'
        f'package {package};\n'
        '\n'
        'service ProbeService {\n'
        '  rpc Probe(ProbeRequest) returns (ProbeResponse);\n'
        '}\n'
        '\n'
        'message ProbeRequest {\n'
        f'  string id = {request_field};\n'
        '}\n'
        '\n'
        'message ProbeResponse {\n'
        '  string id = 1;\n'
        '}\n'
    )


def _module(*, external_field: int = 1, internal_field: int = 1, buf_yaml: str | None = None) -> dict[str, str]:
    """buf.yaml (the repository's own unless given) and one external and one internal contract."""
    return {
        'buf.yaml': BUF_YAML.read_text(encoding='utf-8') if buf_yaml is None else buf_yaml,
        EXTERNAL_PROTO: _contract(EXTERNAL_PACKAGE, external_field),
        INTERNAL_PROTO: _contract(INTERNAL_PACKAGE, internal_field),
    }


def _buf_yaml_with(edit) -> str:
    """The repository's buf.yaml, parsed, changed by `edit`, and written back (its comments are lost)."""
    config = yaml.safe_load(BUF_YAML.read_text(encoding='utf-8'))
    edit(config)
    return yaml.safe_dump(config, sort_keys=False)


# --- the environment, the stand-ins and the scratch repositories -----------------------------------


def _clean_environment() -> dict[str, str]:
    return {
        name: value
        for name, value in os.environ.items()
        if name not in _CALLER_VARIABLES and not name.startswith(_CALLER_PREFIXES)
    }


def _path_without_buf() -> str:
    """PATH with every directory holding a `buf` removed: the pinned one, wherever it was installed."""
    entries = os.environ.get('PATH', '').split(os.pathsep)
    return os.pathsep.join(entry for entry in entries if entry and not os.path.lexists(os.path.join(entry, 'buf')))


@functools.cache
def _pin(name: str) -> str:
    """A Makefile variable as make itself expands it."""
    return _expanded_make_variable(name, REPO_ROOT, _clean_environment())


def _executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding='utf-8')
    path.chmod(0o755)
    return path


def _calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [line.split(SEPARATOR)[:-1] for line in log.read_text(encoding='utf-8').splitlines()]


def _files(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding='utf-8')
        for path in sorted(root.rglob('*'))
        if path.is_file()
    }


def _ran(result: subprocess.CompletedProcess) -> str:
    return f'exit {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}'


def _make(cwd: Path, *arguments: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    """Run the repository's own Makefile from `cwd`."""
    make = shutil.which('make', path=env['PATH'])
    assert make, 'make is not on PATH, so the Makefile cannot be exercised'
    return subprocess.run(
        [make, '--no-print-directory', '-C', str(cwd), '-f', str(MAKEFILE), *arguments],
        capture_output=True,
        text=True,
        env=env,
        timeout=MAKE_TIMEOUT_S,
        check=False,
    )


def _git(cwd: Path, *arguments: str) -> str:
    result = subprocess.run(
        ['git', *arguments],
        cwd=cwd,
        env=_clean_environment() | GIT_ISOLATION,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f'git {" ".join(arguments)} failed in {cwd}: {result.stderr}'
    return result.stdout.strip()


def _write_tree(root: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text, encoding='utf-8')


def _commit(repo: Path, message: str) -> str:
    _git(repo, 'add', '-A')
    _git(repo, 'commit', '-q', '-m', message)
    return _git(repo, 'rev-parse', 'HEAD')


def _repo(root: Path, files: dict[str, str]) -> Path:
    """A git repository at ROOT whose main branch commits FILES."""
    root.mkdir(parents=True)
    _git(root, 'init', '-q', '-b', 'main')
    _write_tree(root, files)
    _commit(root, 'baseline')
    return root


class Stubs:
    """A bin directory of stand-ins, first on a PATH that holds no real buf, and the logs they write."""

    def __init__(self, root: Path) -> None:
        self.bin = root / 'bin'
        self.bin.mkdir()
        self.buf_log = root / 'buf.calls'
        self.uv_log = root / 'uv.calls'
        self.baseline = root / 'baseline-buf-was-given'
        self.path = os.pathsep.join([str(self.bin), _path_without_buf()])
        missing = [tool for tool in RECIPE_TOOLS if not shutil.which(tool, path=self.path)]
        assert not missing, (
            f'{missing} resolve only from a directory that also holds buf, so this PATH cannot run the recipes'
        )
        assert shutil.which('buf', path=self.path) is None

    def install_buf(self) -> 'Stubs':
        _executable(self.bin / 'buf', BUF_STUB)
        return self

    def install_uv(self) -> 'Stubs':
        _executable(self.bin / 'uv', UV_STUB)
        return self

    def env(self, **extra: str) -> dict[str, str]:
        return (
            _clean_environment()
            | GIT_ISOLATION
            | {
                'PATH': self.path,
                'BUF_STUB_LOG': str(self.buf_log),
                'BUF_STUB_VERSION': _pin('BUF_VERSION'),
                'BUF_STUB_BASELINE_COPY': str(self.baseline),
                'UV_STUB_LOG': str(self.uv_log),
            }
            | extra
        )

    def buf_calls(self) -> list[list[str]]:
        return _calls(self.buf_log)

    def uv_calls(self) -> list[list[str]]:
        return _calls(self.uv_log)

    def checks_run(self) -> list[str]:
        """The buf subcommands called, in order, --version included."""
        return [call[0] for call in self.buf_calls()]


@pytest.fixture
def stubs(tmp_path: Path) -> Stubs:
    return Stubs(tmp_path)


@pytest.fixture
def module_repo(tmp_path: Path) -> Path:
    """A repository whose main carries buf.yaml and both contracts, its working tree identical."""
    return _repo(tmp_path / 'repo', _module())


@pytest.fixture(scope='module')
def real_buf() -> str:
    """The pinned buf on PATH. Absent or at another version, the tests that need it FAIL (pytest.ini)."""
    pinned = _pin('BUF_VERSION')
    found = shutil.which('buf')
    assert found, (
        f'buf is not on PATH. These tests run the pinned buf {pinned} on buf.yaml. Install it with '
        f'`make buf-install` (checksum-verified, into ~/.local/bin) until the agent image carries it at '
        f'/usr/local/bin/buf. Never a skip.'
    )
    version = subprocess.run([found, '--version'], capture_output=True, text=True, check=False).stdout.strip()
    assert version == pinned, f'{found} is buf {version!r}, not the pinned {pinned}: run `make buf-install`'
    return found


def _with_real_buf(**extra: str) -> dict[str, str]:
    return _clean_environment() | GIT_ISOLATION | extra


def _not_run_line(target: str, paths: str) -> str:
    return f'{target}: not run: PATHS={paths} does not cover proto/ (buf runs for PATHS=. or proto/...)\n'


# ---------------------------------------------------------------------------------------------------
# THE TARGETS AND THE PIN


@pytest.mark.parametrize('target', NEW_TARGETS)
def test_each_lint_and_buf_target_is_phony_and_documented(target: str):
    """tj-06uflo: .PHONY beside the recipe, or a file named like the target makes it a silent no-op; ## for help."""
    lines = MAKEFILE.read_text(encoding='utf-8').splitlines()
    assert f'.PHONY: {target}' in lines, f'{target} is not declared .PHONY on a line of its own'
    header = [line for line in lines if re.match(rf'^{re.escape(target)}:(?!=)', line)]
    assert len(header) == 1 and re.search(r'\s##\s+\S', header[0]), f'{target} has no ## help line: {header}'
    index = lines.index(f'.PHONY: {target}')
    assert lines[index + 1] == header[0], f'.PHONY: {target} is not directly above its recipe'


def test_the_buf_pin_is_one_version_and_two_checksums():
    """Item 1: a release at or above 1.32.0, the first with buf.yaml v2, and one SHA-256 per pinned platform."""
    version = _pin('BUF_VERSION')
    assert re.fullmatch(r'\d+\.\d+\.\d+', version), version
    assert tuple(map(int, version.split('.'))) >= (1, 32, 0), f'buf {version} predates buf.yaml v2 (1.32.0)'
    sums = [_pin('BUF_SHA256_X86_64'), _pin('BUF_SHA256_AARCH64')]
    assert all(re.fullmatch(r'[0-9a-f]{64}', value) for value in sums), sums
    assert sums[0] != sums[1], 'both platforms pin the same checksum'


def test_buf_install_defaults_to_the_release_page_and_the_users_local_bin(tmp_path: Path):
    """Item 2: the github.com release by default, into ~/.local/bin, the agent image's first PATH entry."""
    env = _clean_environment() | {'HOME': str(tmp_path / 'home')}
    assert _expanded_make_variable('BUF_INSTALL_DIR', tmp_path, env) == f'{tmp_path}/home/.local/bin'
    assert (
        _expanded_make_variable('BUF_RELEASE_URL', tmp_path, env) == 'https://github.com/bufbuild/buf/releases/download'
    )
    assert _expanded_make_variable('BUF', tmp_path, env) == 'buf', 'buf is looked up on PATH unless overridden'
    assert _expanded_make_variable('BUF_AGAINST_REF', tmp_path, env) == 'main'
    assert _expanded_make_variable('BUF_ERROR_FORMAT', tmp_path, env) == 'text'
    assert _expanded_make_variable('BUF_BREAKING_BLOCKING', tmp_path, env) == '0', 'report-only until tj-d2mhru'


# ---------------------------------------------------------------------------------------------------
# GATE 1: make buf-install, against a fake release served over file://


def _fake_buf(version: str, status: int = 0, tag: str = '') -> bytes:
    return f'#!/bin/sh\n# fake buf {tag}\necho {version}\nexit {status}\n'.encode()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


PREVIOUS_BUF = b'#!/bin/sh\necho previous\n'


class Release:
    """A fake release tree, a uname that reports any platform, and an install directory nothing else uses."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / 'bin'
        self.bin.mkdir()
        self.install_dir = root / 'install' / 'bin'
        self.version_dir = root / 'release' / f'v{_pin("BUF_VERSION")}'
        self.version_dir.mkdir(parents=True)
        self.curl_log = root / 'curl.calls'
        path = _path_without_buf()
        curl = shutil.which('curl', path=path)
        assert curl, 'curl is not on PATH, so buf-install cannot fetch even a file:// release'
        _executable(self.bin / 'uname', UNAME_STUB)
        _executable(self.bin / 'curl', CURL_WRAPPER.format(curl=curl))
        self.path = os.pathsep.join([str(self.bin), path])

    def asset(self, name: str, data: bytes) -> None:
        (self.version_dir / name).write_bytes(data)

    def existing(self, data: bytes) -> None:
        self.install_dir.mkdir(parents=True)
        (self.install_dir / 'buf').write_bytes(data)

    def install(self, *arguments: str, platform: tuple[str, str] = ('Linux', 'x86_64')) -> subprocess.CompletedProcess:
        env = _clean_environment() | {
            'PATH': self.path,
            'HOME': str(self.root / 'home'),
            'UNAME_S': platform[0],
            'UNAME_M': platform[1],
            'CURL_LOG': str(self.curl_log),
        }
        return _make(
            self.root,
            'buf-install',
            f'BUF_RELEASE_URL={(self.root / "release").as_uri()}',
            f'BUF_INSTALL_DIR={self.install_dir}',
            *arguments,
            env=env,
        )

    def installed(self) -> dict[str, bytes]:
        """Every entry in the install directory, temporary files included, with its bytes."""
        if not self.install_dir.exists():
            return {}
        return {path.name: path.read_bytes() for path in self.install_dir.iterdir()}

    def curl_calls(self) -> list[list[str]]:
        return _calls(self.curl_log)


@pytest.fixture
def release(tmp_path: Path) -> Release:
    return Release(tmp_path)


def test_a_release_matching_the_pin_is_installed_and_runs(release: Release):
    """Item 2: fetched into a temporary file INSIDE the install directory, verified, renamed over buf."""
    fake = _fake_buf(_pin('BUF_VERSION'))
    release.asset('buf-Linux-x86_64', fake)
    release.existing(PREVIOUS_BUF)
    result = release.install(f'BUF_SHA256_X86_64={_sha256(fake)}')
    assert result.returncode == 0, _ran(result)
    assert release.installed() == {'buf': fake}, 'the old buf was not replaced, or a temporary file was left behind'
    installed = release.install_dir / 'buf'
    # Run, not stat: an install that skipped the chmod leaves mktemp's 0600, which not even root can exec.
    ran = subprocess.run([str(installed), '--version'], capture_output=True, text=True, check=False)
    assert ran.stdout.strip() == _pin('BUF_VERSION'), ran
    assert f'installed buf {_pin("BUF_VERSION")} at {installed}' in result.stdout, _ran(result)
    ((url, output),) = [(call[-1], call[call.index('-o') + 1]) for call in release.curl_calls()]
    assert url == f'{(release.root / "release").as_uri()}/v{_pin("BUF_VERSION")}/buf-Linux-x86_64'
    assert Path(output).parent == release.install_dir, (
        f'curl wrote to {output}, outside {release.install_dir}: the final rename is atomic only within one directory'
    )


@pytest.mark.parametrize(
    ('machine', 'asset', 'checksum'),
    [('x86_64', 'buf-Linux-x86_64', 'BUF_SHA256_X86_64'), ('aarch64', 'buf-Linux-aarch64', 'BUF_SHA256_AARCH64')],
)
def test_each_platform_fetches_its_own_asset_against_its_own_checksum(
    release: Release, machine: str, asset: str, checksum: str
):
    """Linux x86_64 and aarch64 each take their own binary and their own SHA-256, never the other's."""
    fakes = {name: _fake_buf(_pin('BUF_VERSION'), tag=name) for name in ('buf-Linux-x86_64', 'buf-Linux-aarch64')}
    for name, data in fakes.items():
        release.asset(name, data)
    other = ({'BUF_SHA256_X86_64', 'BUF_SHA256_AARCH64'} - {checksum}).pop()

    crossed = release.install(f'{other}={_sha256(fakes[asset])}', platform=('Linux', machine))
    assert crossed.returncode != 0 and 'checksum mismatch' in crossed.stderr, _ran(crossed)
    assert release.installed() == {}

    result = release.install(f'{checksum}={_sha256(fakes[asset])}', platform=('Linux', machine))
    assert result.returncode == 0, _ran(result)
    assert release.installed() == {'buf': fakes[asset]}


@pytest.mark.parametrize('previous', [None, PREVIOUS_BUF], ids=['no-buf-yet', 'an-existing-buf'])
def test_a_checksum_mismatch_is_refused_and_leaves_nothing_behind(release: Release, previous: bytes | None):
    """Item 2 / gate 1: the download is deleted, nothing is installed, and an existing buf is byte-identical."""
    fake = _fake_buf(_pin('BUF_VERSION'))
    release.asset('buf-Linux-x86_64', fake)
    if previous is not None:
        release.existing(previous)
    result = release.install()
    assert result.returncode != 0, _ran(result)
    assert 'checksum mismatch' in result.stderr, _ran(result)
    assert f'expected {_pin("BUF_SHA256_X86_64")}, got {_sha256(fake)}' in result.stderr, _ran(result)
    assert release.installed() == ({} if previous is None else {'buf': previous})


@pytest.mark.parametrize(
    ('fake', 'named'),
    [(_fake_buf('9.9.9'), 'reports version 9.9.9'), (_fake_buf('1.73.0', status=3), 'a failing --version')],
    ids=['another-version', 'a-failing-version-check'],
)
def test_a_release_that_does_not_report_the_pinned_version_is_refused(release: Release, fake: bytes, named: str):
    """Checked before the rename, so a pin whose version and checksum disagree leaves the old buf in place."""
    release.asset('buf-Linux-x86_64', fake)
    release.existing(PREVIOUS_BUF)
    result = release.install(f'BUF_SHA256_X86_64={_sha256(fake)}')
    assert result.returncode != 0, _ran(result)
    assert named in result.stderr and _pin('BUF_VERSION') in result.stderr, _ran(result)
    assert release.installed() == {'buf': PREVIOUS_BUF}


def test_a_missing_release_asset_is_refused(release: Release):
    """Curl -f fails the download, and the trap still removes its temporary file."""
    release.existing(PREVIOUS_BUF)
    result = release.install()
    assert result.returncode != 0, _ran(result)
    assert len(release.curl_calls()) == 1, 'the recipe never tried the download this case is about'
    assert release.installed() == {'buf': PREVIOUS_BUF}


@pytest.mark.parametrize('platform', [('Darwin', 'arm64'), ('Linux', 'riscv64'), ('Linux', 'armv7l')], ids='-'.join)
def test_an_unpinned_platform_fails_naming_itself_before_any_download(release: Release, platform: tuple[str, str]):
    """Never a checksum mismatch, which would read like a corrupted download."""
    release.asset('buf-Linux-x86_64', _fake_buf(_pin('BUF_VERSION')))
    release.existing(PREVIOUS_BUF)
    result = release.install(platform=platform)
    assert result.returncode != 0, _ran(result)
    assert f'no buf checksum pinned for {platform[0]} {platform[1]}' in result.stderr, _ran(result)
    assert 'checksum mismatch' not in result.stderr
    assert release.curl_calls() == [], 'an unpinned platform downloaded something'
    assert release.installed() == {'buf': PREVIOUS_BUF}


# ---------------------------------------------------------------------------------------------------
# GATES 2 AND 3: THE PATHS SELECTOR, AND THE PIN CHECK WHEN IT SELECTS

SELECTING = [
    '.',
    './',
    '',
    'proto',
    'proto/',
    './proto',
    './proto/',
    'proto/trader_joe',
    'proto/trader_joe/proto/ping/v1/ping.proto',
    'common proto',
    'proto common',
    'common ./proto/',
]
NOT_SELECTING = [
    'common',
    'data/ingest',
    'common/tests',
    'common data/ingest',
    'routers/data_store',
    'gen/proto',
    'common/proto',
    'protobuf',
    'proto_extra',
    '../proto',
    '.github',
]


@pytest.mark.parametrize('target', ['lint-proto', 'lint-fix-proto'])
@pytest.mark.parametrize('paths', NOT_SELECTING)
def test_a_scope_that_misses_proto_prints_one_line_and_never_calls_buf(
    stubs: Stubs, tmp_path: Path, target: str, paths: str
):
    """Item 4: one line, exit 0, buf untouched -- whether buf is absent or present.

    So every builder's component-scoped hand-back behaves as it did before buf.
    """
    absent = _make(tmp_path, target, f'PATHS={paths}', env=stubs.env())
    assert (absent.returncode, absent.stdout, absent.stderr) == (0, _not_run_line(target, paths), ''), _ran(absent)
    stubs.install_buf()
    present = _make(tmp_path, target, f'PATHS={paths}', env=stubs.env())
    assert (present.returncode, present.stdout) == (0, _not_run_line(target, paths)), _ran(present)
    assert stubs.buf_calls() == [], 'a scope that does not cover proto/ ran buf'


@pytest.mark.parametrize('target', ['lint-proto', 'lint-fix-proto'])
@pytest.mark.parametrize('paths', SELECTING)
def test_a_scope_covering_proto_fails_without_buf_naming_the_pin_and_the_remedy(
    stubs: Stubs, tmp_path: Path, target: str, paths: str
):
    """Items 4 and gate 3: selected, an absent buf FAILS -- never a skip -- and says how to get the pin."""
    result = _make(tmp_path, target, f'PATHS={paths}', env=stubs.env())
    assert result.returncode != 0, _ran(result)
    for phrase in ('buf is not on PATH', f'needs buf {_pin("BUF_VERSION")}', 'make buf-install', 'agent image'):
        assert phrase in result.stderr, f'the refusal does not say {phrase!r}:\n{_ran(result)}'


@pytest.mark.parametrize(
    ('variables', 'named'),
    [
        ({'BUF_STUB_VERSION': '1.72.0'}, 'found buf 1.72.0 at'),
        ({'BUF_STUB_VERSION_EXIT': '2'}, 'whose --version fails'),
    ],
    ids=['another-version', 'a-failing-version-check'],
)
@pytest.mark.parametrize('target', ['lint-proto', 'lint-fix-proto'])
def test_a_buf_at_another_version_fails_before_it_checks_anything(
    stubs: Stubs, module_repo: Path, target: str, variables: dict[str, str], named: str
):
    """Gate 3: another version may lint, format or compare differently, so it fails, and nothing else runs."""
    stubs.install_buf()
    result = _make(module_repo, target, 'PATHS=proto', env=stubs.env(**variables))
    assert result.returncode != 0, _ran(result)
    assert named in result.stderr and 'make buf-install' in result.stderr, _ran(result)
    assert stubs.buf_calls() == [['--version']]


# ---------------------------------------------------------------------------------------------------
# GATE 4: WHAT THE SELECTED LEG RUNS, AND THE REPORT-ONLY DOWNGRADE


@pytest.mark.parametrize('paths', ['.', 'proto'])
def test_lint_proto_runs_lint_then_format_then_both_builds_then_breaking(stubs: Stubs, module_repo: Path, paths: str):
    """Item 3: always the WHOLE module, in order; the baseline is a temporary directory, gone afterwards."""
    stubs.install_buf()
    result = _make(module_repo, 'lint-proto', f'PATHS={paths}', env=stubs.env())
    assert result.returncode == 0, _ran(result)
    calls = stubs.buf_calls()
    assert [call[0] for call in calls] == ['--version', 'lint', 'format', 'build', 'build', 'breaking'], calls
    baseline = calls[4][1]
    assert calls == [
        ['--version'],
        ['lint', '--error-format=text'],
        ['format', '--diff', '--exit-code'],
        ['build'],
        ['build', baseline],
        ['breaking', '--error-format=text', '--against', baseline],
    ]
    assert Path(baseline).is_absolute() and not Path(baseline).exists(), f'the baseline {baseline} was left behind'
    assert result.stdout.splitlines()[-1] == 'lint-proto: breaking: no breaking changes against main', _ran(result)


def test_ci_error_format_reaches_buf_lint_and_buf_breaking(stubs: Stubs, module_repo: Path):
    """Item 5: BUF_ERROR_FORMAT=github-actions, CI's, turns lint and breaking findings into annotations."""
    stubs.install_buf()
    result = _make(module_repo, 'lint-proto', 'BUF_ERROR_FORMAT=github-actions', env=stubs.env())
    assert result.returncode == 0, _ran(result)
    calls = {call[0]: call for call in stubs.buf_calls()}
    assert '--error-format=github-actions' in calls['lint'] and '--error-format=github-actions' in calls['breaking']


def test_lint_fix_proto_applies_buf_format(stubs: Stubs, module_repo: Path):
    """Item 3: lint checks buf format, so lint-fix writes it; buf lint and breaking have no autofix."""
    stubs.install_buf()
    result = _make(module_repo, 'lint-fix-proto', 'PATHS=proto', env=stubs.env())
    assert result.returncode == 0, _ran(result)
    assert stubs.buf_calls() == [['--version'], ['format', '-w']]


@pytest.mark.parametrize(
    ('arguments', 'ruff', 'checks'),
    [
        (['lint', 'PATHS=.'], [['run', 'ruff', 'check', '.'], ['run', 'ruff', 'format', '--check', '.']], 'lint'),
        (
            ['lint', 'PATHS=common'],
            [['run', 'ruff', 'check', 'common'], ['run', 'ruff', 'format', '--check', 'common']],
            None,
        ),
        (
            ['lint-fix', 'PATHS=proto'],
            [['run', 'ruff', 'check', '--fix', 'proto'], ['run', 'ruff', 'format', 'proto']],
            'lint-fix',
        ),
        (
            ['lint-fix', 'PATHS=data/ingest'],
            [['run', 'ruff', 'check', '--fix', 'data/ingest'], ['run', 'ruff', 'format', 'data/ingest']],
            None,
        ),
    ],
    ids=['lint-everything', 'lint-a-component', 'lint-fix-proto', 'lint-fix-a-component'],
)
def test_lint_and_lint_fix_run_ruff_as_before_and_the_proto_leg_when_selected(
    stubs: Stubs, module_repo: Path, arguments: list[str], ruff: list[list[str]], checks: str | None
):
    """Item 3: one entry point, one leg per language. lint-python is the two ruff lines lint always ran."""
    stubs.install_buf().install_uv()
    env = stubs.env()
    marker = _expanded_make_variable('VENV_MARKER', module_repo, env)
    result = _make(module_repo, '-o', marker, *arguments, env=env)
    assert result.returncode == 0, _ran(result)
    assert stubs.uv_calls() == ruff
    expected = {
        'lint': ['--version', 'lint', 'format', 'build', 'build', 'breaking'],
        'lint-fix': ['--version', 'format'],
        None: [],
    }[checks]
    assert stubs.checks_run() == expected, stubs.buf_calls()
    if checks is None:
        assert result.stdout.splitlines()[-1].endswith('does not cover proto/ (buf runs for PATHS=. or proto/...)')


BANNER = 'REPORT-ONLY until the first SDK release (tj-d2mhru)'
_BREAKING_OUTCOMES = {
    'no-findings': ('0', None, 0, 'lint-proto: breaking: no breaking changes against main'),
    'no-findings-blocking': ('0', '1', 0, 'lint-proto: breaking: no breaking changes against main'),
    'findings-report-only-by-default': ('100', None, 0, BANNER),
    'findings-report-only': ('100', '0', 0, BANNER),
    'findings-blocking': ('100', '1', 1, 'BLOCKING (BUF_BREAKING_BLOCKING=1)'),
    'broken-check-report-only': ('1', None, 1, 'a broken check, not a finding; failing in either mode'),
    'broken-check-blocking': ('1', '1', 1, 'a broken check, not a finding; failing in either mode'),
    'another-failure-code': ('2', None, 1, 'a broken check, not a finding; failing in either mode'),
}


@pytest.mark.parametrize(
    ('exit_code', 'blocking', 'fails', 'says'), _BREAKING_OUTCOMES.values(), ids=_BREAKING_OUTCOMES
)
def test_breaking_findings_are_report_only_and_a_broken_check_always_fails(
    stubs: Stubs, module_repo: Path, exit_code: str, blocking: str | None, fails: int, says: str
):
    """Item 5 (W2): exit 100 is findings, printed under a banner, passed while BLOCKING is 0, failed at 1.

    Any other non-zero is a broken check and fails in both modes, which is why this is not `|| true`.
    """
    stubs.install_buf()
    arguments = [] if blocking is None else [f'BUF_BREAKING_BLOCKING={blocking}']
    result = _make(module_repo, 'lint-proto', *arguments, env=stubs.env(BUF_STUB_BREAKING_EXIT=exit_code))
    assert (result.returncode != 0) == bool(fails), _ran(result)
    assert says in result.stdout + result.stderr, _ran(result)
    if exit_code == '100':
        output = result.stdout + result.stderr
        assert output.index('stub finding') < output.index('the breaking change(s) above'), (
            'the findings must print first'
        )


@pytest.mark.parametrize(
    ('variables', 'reached', 'says'),
    [
        ({'BUF_STUB_LINT_EXIT': '100'}, ['--version', 'lint'], ''),
        ({'BUF_STUB_FORMAT_EXIT': '100'}, ['--version', 'lint', 'format'], "apply it with 'make lint-fix PATHS=proto'"),
    ],
    ids=['a-lint-finding', 'a-format-diff'],
)
def test_lint_and_format_findings_fail_even_while_breaking_is_report_only(
    stubs: Stubs, module_repo: Path, variables: dict[str, str], reached: list[str], says: str
):
    """Report-only is breaking's alone: a lint finding or a format diff fails, and nothing after it runs."""
    stubs.install_buf()
    result = _make(module_repo, 'lint-proto', env=stubs.env(**variables))
    assert result.returncode != 0, _ran(result)
    assert stubs.checks_run() == reached
    assert says in result.stderr, _ran(result)


@pytest.mark.parametrize(
    ('variables', 'says'),
    [
        ({'BUF_STUB_BUILD_EXIT': '100'}, ''),
        (
            {'BUF_STUB_BASELINE_BUILD_EXIT': '100'},
            'does not build (exit 100); failing, because a broken baseline is not a finding',
        ),
    ],
    ids=['the-working-tree', 'the-baseline'],
)
def test_a_side_that_does_not_build_fails_even_report_only(
    stubs: Stubs, module_repo: Path, variables: dict[str, str], says: str
):
    """Buf 1.73.0 exits 100 for a compile error as well as for findings, so breaking never runs on one."""
    stubs.install_buf()
    result = _make(module_repo, 'lint-proto', env=stubs.env(**variables))
    assert result.returncode != 0, _ran(result)
    assert 'breaking' not in stubs.checks_run(), stubs.buf_calls()
    assert says in result.stderr, _ran(result)
    if 'BUF_STUB_BASELINE_BUILD_EXIT' in variables:
        assert not Path(stubs.buf_calls()[-1][1]).exists(), 'a failed run left its baseline directory behind'


# ---------------------------------------------------------------------------------------------------
# GATE 6: THE BASELINE, MATERIALISED WITH git archive


def test_a_ref_with_no_proto_is_nothing_to_compare_and_passes(stubs: Stubs, tmp_path: Path):
    """The first-adoption case: main has no proto/ until PR 2 merges. Said out loud, never silent."""
    repo = _repo(tmp_path / 'repo', {'README.md': 'readme\n'})
    _write_tree(repo, _module())
    stubs.install_buf()
    result = _make(repo, 'lint-proto', env=stubs.env())
    assert result.returncode == 0, _ran(result)
    assert 'lint-proto: breaking: no proto/ on main: nothing to compare' in result.stdout.splitlines(), _ran(result)
    assert stubs.checks_run() == ['--version', 'lint', 'format']


def test_a_ref_that_does_not_resolve_fails(stubs: Stubs, module_repo: Path):
    """A missing baseline is a broken check, not a pass, in either mode."""
    stubs.install_buf()
    result = _make(module_repo, 'lint-proto', 'BUF_AGAINST_REF=no-such-ref', env=stubs.env())
    assert result.returncode != 0, _ran(result)
    assert "the baseline ref 'no-such-ref' does not resolve" in result.stderr, _ran(result)
    assert 'BUF_AGAINST_REF=' in result.stderr
    assert stubs.checks_run() == ['--version', 'lint', 'format']


def test_a_ref_with_proto_but_no_buf_yaml_fails(stubs: Stubs, tmp_path: Path):
    """There is no module to compare against, which is not the same as nothing to compare."""
    module = _module()
    repo = _repo(tmp_path / 'repo', {name: text for name, text in module.items() if name != 'buf.yaml'})
    _write_tree(repo, {'buf.yaml': module['buf.yaml']})
    stubs.install_buf()
    result = _make(repo, 'lint-proto', env=stubs.env())
    assert result.returncode != 0, _ran(result)
    assert 'main has proto/ but no buf.yaml' in result.stderr, _ran(result)
    assert 'breaking' not in stubs.checks_run()


def test_the_baseline_is_the_refs_buf_yaml_and_proto_never_the_working_tree(stubs: Stubs, module_repo: Path):
    """Buf is handed <ref>'s buf.yaml and proto/ and nothing else; uncommitted edits and new files stay out."""
    committed = _module()
    _write_tree(
        module_repo,
        {
            'buf.yaml': committed['buf.yaml'] + '# an uncommitted edit\n',
            EXTERNAL_PROTO: _contract(EXTERNAL_PACKAGE, 2),
            'proto/trader_joe/proto/extra/v1/extra.proto': _contract('trader_joe.proto.extra.v1'),
            'README.md': 'not in the archive\n',
        },
    )
    stubs.install_buf()
    result = _make(module_repo, 'lint-proto', env=stubs.env())
    assert result.returncode == 0, _ran(result)
    assert _files(stubs.baseline) == committed
    sha = _git(module_repo, 'rev-parse', '--short', 'main^{commit}')
    assert (
        f'lint-proto: breaking: against main ({sha}), its buf.yaml and proto/ extracted by git archive' in result.stdout
    )


def test_the_against_ref_picks_the_baseline(stubs: Stubs, module_repo: Path):
    """BUF_AGAINST_REF=<ref> compares against that ref's tree, as CI's origin/main does."""
    _git(module_repo, 'checkout', '-q', '-b', 'release')
    _write_tree(module_repo, {EXTERNAL_PROTO: _contract(EXTERNAL_PACKAGE, 7)})
    _commit(module_repo, 'release')
    _git(module_repo, 'checkout', '-q', 'main')
    stubs.install_buf()
    result = _make(module_repo, 'lint-proto', 'BUF_AGAINST_REF=release', env=stubs.env())
    assert result.returncode == 0, _ran(result)
    assert _files(stubs.baseline)[EXTERNAL_PROTO] == _contract(EXTERNAL_PACKAGE, 7)
    assert result.stdout.splitlines()[-1] == 'lint-proto: breaking: no breaking changes against release'


def test_no_git_attribute_thins_or_rewrites_the_archived_baseline():
    """An export-ignore or export-subst on buf.yaml or proto/ would change what git archive hands buf.

    Silently: a file dropped from the baseline makes every change to it read as an addition, which
    breaking never reports. Read from the repository's own attributes, which become main's at merge.
    """
    tracked = subprocess.run(
        ['git', 'ls-files', '--', 'buf.yaml', 'proto'], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    assert 'buf.yaml' in tracked and any(path.endswith('.proto') for path in tracked), tracked
    attributes = subprocess.run(
        ['git', 'check-attr', 'export-ignore', 'export-subst', '--', *tracked],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert len(attributes) == 2 * len(tracked), attributes
    archived_differently = [line for line in attributes if not line.endswith(': unspecified')]
    assert not archived_differently, f'git archive would drop or rewrite these in the baseline: {archived_differently}'


def test_the_baseline_resolves_from_a_linked_worktree(stubs: Stubs, module_repo: Path, tmp_path: Path):
    """Item 5's reason for git archive: in an agent's worktree .git is a FILE, and local main still resolves."""
    worktree = tmp_path / 'worktree'
    _git(module_repo, 'worktree', 'add', '-q', '-b', 'feature', str(worktree))
    assert (worktree / '.git').is_file()
    _write_tree(worktree, {EXTERNAL_PROTO: _contract(EXTERNAL_PACKAGE, 2)})
    stubs.install_buf()
    result = _make(worktree, 'lint-proto', env=stubs.env())
    assert result.returncode == 0, _ran(result)
    assert _files(stubs.baseline) == _module()


# ---------------------------------------------------------------------------------------------------
# GATE 5: THE CONFIGURATION, WITH THE REAL PINNED buf


def test_buf_yaml_is_the_ruled_configuration_and_nothing_more():
    """Item 6: v2, module root proto/, STANDARD bare, comment ignores off, FILE ignoring internal/.

    The ignore path is relative to buf.yaml (relative to the module it silently matches nothing). Any
    other key is an exemption, and an exemption needs a Q:@architect first.
    """
    assert yaml.safe_load(BUF_YAML.read_text(encoding='utf-8')) == {
        'version': 'v2',
        'modules': [{'path': 'proto'}],
        'lint': {'use': ['STANDARD'], 'disallow_comment_ignores': True},
        'breaking': {'use': ['FILE'], 'ignore': ['proto/trader_joe/proto/internal']},
    }


def _buf(real_buf: str, cwd: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [real_buf, *arguments], cwd=cwd, capture_output=True, text=True, timeout=MAKE_TIMEOUT_S, check=False
    )


def _findings(result: subprocess.CompletedProcess) -> list[dict]:
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


_LINT_CASES = {
    'a-package-in-the-wrong-directory': (
        {EXTERNAL_PROTO: _contract('trader_joe.proto.elsewhere.v1')},
        'PACKAGE_DIRECTORY_MATCH',
    ),
    'an-unprefixed-enum-value': ({EXTERNAL_PROTO: _contract(EXTERNAL_PACKAGE) + UNPREFIXED_ENUM}, 'ENUM_VALUE_PREFIX'),
    'an-unprefixed-enum-value-under-a-lint-ignore-comment': (
        {EXTERNAL_PROTO: _contract(EXTERNAL_PACKAGE) + IGNORED_UNPREFIXED_ENUM},
        'ENUM_VALUE_PREFIX',
    ),
}


def test_a_module_in_the_ruled_layout_passes_buf_lint_and_buf_format(real_buf: str, tmp_path: Path):
    """The control for the cases below: with the module rooted at proto/, a correct contract has no finding."""
    _write_tree(tmp_path, _module())
    lint = _buf(real_buf, tmp_path, 'lint', '--error-format=json')
    assert (lint.returncode, _findings(lint)) == (0, []), _ran(lint)
    formatted = _buf(real_buf, tmp_path, 'format', '--diff', '--exit-code')
    assert formatted.returncode == 0, _ran(formatted)


@pytest.mark.parametrize(('files', 'rule'), _LINT_CASES.values(), ids=_LINT_CASES)
def test_standard_rejects_and_no_comment_exempts(real_buf: str, tmp_path: Path, files: dict[str, str], rule: str):
    """Gate 5: each fails buf lint with exit 100 under the committed buf.yaml, a buf:lint:ignore included."""
    _write_tree(tmp_path, _module() | files)
    result = _buf(real_buf, tmp_path, 'lint', '--error-format=json')
    assert result.returncode == 100, _ran(result)
    assert rule in {finding['type'] for finding in _findings(result)}, _ran(result)


def test_the_lint_ignore_comment_would_exempt_were_comment_ignores_on(real_buf: str, tmp_path: Path):
    """The control: the comment is placed where buf honours it, so the case above fails on the config's say-so."""
    buf_yaml = _buf_yaml_with(lambda config: config['lint'].update(disallow_comment_ignores=False))
    _write_tree(
        tmp_path, _module(buf_yaml=buf_yaml) | {EXTERNAL_PROTO: _contract(EXTERNAL_PACKAGE) + IGNORED_UNPREFIXED_ENUM}
    )
    result = _buf(real_buf, tmp_path, 'lint', '--error-format=json')
    assert (result.returncode, _findings(result)) == (0, []), _ran(result)


def _drop_breaking_ignore(config: dict) -> None:
    del config['breaking']['ignore']


_BREAKING_CASES = {
    'an-external-field-renumbered': ({'external_field': 2}, None, 100, {EXTERNAL_PROTO}),
    'an-internal-field-renumbered': ({'internal_field': 2}, None, 0, set()),
    'both-renumbered': ({'external_field': 2, 'internal_field': 2}, None, 100, {EXTERNAL_PROTO}),
    # The control: the internal change IS a break, which only the ignore keeps out of the report.
    'an-internal-field-renumbered-without-the-ignore': (
        {'internal_field': 2},
        _drop_breaking_ignore,
        100,
        {INTERNAL_PROTO},
    ),
}


@pytest.mark.parametrize(('change', 'edit', 'status', 'reported'), _BREAKING_CASES.values(), ids=_BREAKING_CASES)
def test_file_reports_an_external_break_and_ignores_internal(
    real_buf: str, tmp_path: Path, change: dict[str, int], edit, status: int, reported: set[str]
):
    """Gate 5: FILE reports a renumbered field with exit 100; internal/ is ignored, and only internal/."""
    buf_yaml = None if edit is None else _buf_yaml_with(edit)
    baseline, current = tmp_path / 'baseline', tmp_path / 'current'
    _write_tree(baseline, _module(buf_yaml=buf_yaml))
    _write_tree(current, _module(buf_yaml=buf_yaml, **change))
    result = _buf(real_buf, current, 'breaking', '--error-format=json', '--against', str(baseline))
    assert result.returncode == status, _ran(result)
    assert {finding['path'] for finding in _findings(result)} == reported, _ran(result)


def test_make_lint_reports_a_real_break_and_blocking_fails_it(real_buf: str, tmp_path: Path):
    """Done-when: a deliberate break shown REPORT-ONLY, then failing with BUF_BREAKING_BLOCKING=1, CI annotations too."""
    repo = _repo(tmp_path / 'repo', _module())
    _write_tree(repo, {EXTERNAL_PROTO: _contract(EXTERNAL_PACKAGE, 2)})
    reported = _make(repo, 'lint-proto', 'PATHS=proto', env=_with_real_buf())
    assert reported.returncode == 0, _ran(reported)
    assert BANNER in reported.stdout and EXTERNAL_PROTO in reported.stdout, _ran(reported)
    blocking = _make(repo, 'lint-proto', 'PATHS=proto', 'BUF_BREAKING_BLOCKING=1', env=_with_real_buf())
    assert blocking.returncode != 0 and 'BLOCKING' in blocking.stderr, _ran(blocking)
    annotated = _make(repo, 'lint-proto', 'BUF_ERROR_FORMAT=github-actions', env=_with_real_buf())
    assert annotated.returncode == 0, _ran(annotated)
    assert f'::error file={EXTERNAL_PROTO},' in annotated.stdout, _ran(annotated)


def test_make_lint_passes_an_internal_break_even_blocking(real_buf: str, tmp_path: Path):
    """The internal/ ignore proof through make: no finding, so nothing to report or block."""
    repo = _repo(tmp_path / 'repo', _module())
    _write_tree(repo, {INTERNAL_PROTO: _contract(INTERNAL_PACKAGE, 2)})
    result = _make(repo, 'lint-proto', 'BUF_BREAKING_BLOCKING=1', env=_with_real_buf())
    assert result.returncode == 0, _ran(result)
    assert result.stdout.splitlines()[-1] == 'lint-proto: breaking: no breaking changes against main', _ran(result)


def test_a_baseline_that_does_not_compile_fails_even_report_only(real_buf: str, tmp_path: Path):
    """Real buf exits 100 when the --against side does not compile, the findings code; the build makes it fail."""
    module = _module()
    repo = _repo(tmp_path / 'repo', module | {EXTERNAL_PROTO: module[EXTERNAL_PROTO] + 'message Broken {\n'})
    _write_tree(repo, module)
    result = _make(repo, 'lint-proto', env=_with_real_buf())
    assert result.returncode != 0, _ran(result)
    assert 'the baseline on main does not build' in result.stderr, _ran(result)
    assert BANNER not in result.stdout


def test_a_lint_finding_fails_make_lint_with_the_real_buf(real_buf: str, tmp_path: Path):
    """Report-only never reaches buf lint: an unprefixed enum value fails the leg."""
    repo = _repo(tmp_path / 'repo', _module())
    _write_tree(repo, {EXTERNAL_PROTO: _contract(EXTERNAL_PACKAGE) + UNPREFIXED_ENUM})
    result = _make(repo, 'lint-proto', env=_with_real_buf())
    assert result.returncode != 0, _ran(result)
    assert 'breaking:' not in result.stdout, 'breaking ran after a lint finding'


def test_lint_fix_writes_what_lint_checks(real_buf: str, tmp_path: Path):
    """An unformatted file fails lint-proto with the fix named; lint-fix-proto writes buf's format; lint passes."""
    repo = _repo(tmp_path / 'repo', _module())
    canonical = _contract(EXTERNAL_PACKAGE)
    _write_tree(repo, {EXTERNAL_PROTO: canonical.replace('  string id = 1;', '      string    id = 1 ;')})
    failed = _make(repo, 'lint-proto', env=_with_real_buf())
    assert failed.returncode != 0 and "apply it with 'make lint-fix PATHS=proto'" in failed.stderr, _ran(failed)
    fixed = _make(repo, 'lint-fix-proto', 'PATHS=proto', env=_with_real_buf())
    assert fixed.returncode == 0, _ran(fixed)
    assert (repo / EXTERNAL_PROTO).read_text(encoding='utf-8') == canonical
    passed = _make(repo, 'lint-proto', env=_with_real_buf())
    assert passed.returncode == 0, _ran(passed)


# ---------------------------------------------------------------------------------------------------
# CI'S BASELINE, END TO END: the workflow's own fetch step, then its own make lint arguments


def test_ci_fetch_step_then_lint_arguments_compare_a_shallow_checkout_with_main(real_buf: str, tmp_path: Path):
    """Item 7 on a local origin: CI's own fetch script, then its own make lint arguments.

    A depth-1 checkout of a branch has no main; the fetch step makes origin/main, and the lint step's
    arguments compare against it and annotate the break. What this cannot show is GitHub's side: the
    runner's checkout and the annotations rendering on a pull request.
    """
    origin = _repo(tmp_path / 'origin', _module())
    main_sha = _git(origin, 'rev-parse', 'main')
    _git(origin, 'checkout', '-q', '-b', 'feature')
    _write_tree(origin, {EXTERNAL_PROTO: _contract(EXTERNAL_PACKAGE, 2)})
    _commit(origin, 'a break')
    clone = tmp_path / 'clone'
    _git(tmp_path, 'clone', '-q', '--depth=1', '--branch', 'feature', origin.as_uri(), str(clone))
    env = _clean_environment() | GIT_ISOLATION
    before = subprocess.run(
        ['git', 'rev-parse', '--verify', '--quiet', 'refs/remotes/origin/main'], cwd=clone, env=env, check=False
    )
    assert before.returncode != 0, 'the clone already has origin/main, so the fetch step would prove nothing'

    _, steps = _lint_job_steps()
    script = tmp_path / 'fetch.sh'
    script.write_text(
        steps[_only_step(steps, lambda step: bool(_fetched_refs(step)), 'fetches main')]['run'], encoding='utf-8'
    )
    fetched = subprocess.run(
        ['bash', '--noprofile', '--norc', '-e', str(script)],
        cwd=clone,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert fetched.returncode == 0, _ran(fetched)
    assert _git(clone, 'rev-parse', 'refs/remotes/origin/main') == main_sha
    assert _git(clone, 'rev-parse', '--is-shallow-repository') == 'true'

    variables = [word for word in _make_lint_command(steps)[1:] if '=' in word]
    result = _make(clone, 'lint-proto', *variables, env=_with_real_buf())
    assert result.returncode == 0, _ran(result)
    assert 'lint-proto: breaking: against origin/main' in result.stdout, _ran(result)
    assert f'::error file={EXTERNAL_PROTO},' in result.stdout and BANNER in result.stdout, _ran(result)


# ---------------------------------------------------------------------------------------------------
# GATE 9: THE REPOSITORY'S OWN TREE


def test_the_repository_proto_tree_passes_the_proto_leg(real_buf: str):
    """Buf lint and buf format on the committed tree, compared with itself so CI's missing local main is moot."""
    result = _make(REPO_ROOT, 'lint-proto', 'PATHS=.', 'BUF_AGAINST_REF=HEAD', env=_clean_environment())
    assert result.returncode == 0, _ran(result)
    assert 'buf lint --error-format=text' in result.stdout.splitlines(), _ran(result)
