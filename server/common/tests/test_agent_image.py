"""build_infra pins for the agent image, .devcontainer/Dockerfile (tj-qenrpk; commit 62a15e0, bead tj-gys6xn).

The image installs Claude Code with the native installer, run as the agent user after the USER
switch, so the binary lands in ~/.local/bin, owned by the user who runs it, and Claude can update
itself in place. Before 62a15e0 it was a root `npm install -g`, which the updater could not write
to. An earlier attempt declared the dev container feature instead, which a plain `docker compose
build` never applies, and the image came out with no Claude at all -- silently (tj-gys6xn's notes).
Nothing can build the image here (the agent container has no Docker), so these read the Dockerfile.

Five properties, each one a silent failure if lost:

* THE VERSION. The installer's version argument is the image's starting version, and the
  Dockerfile's own comment names .claude/workflow.yml's claude_code pin as "what to bump". Two
  places, one value, and nothing compared them.
* THE USER. Run before USER, or as root, the install is root-owned again and self-update stops.
* THE PATH. The entrypoint execs its command and no login shell runs, so the line the installer
  appends to ~/.bashrc is never read: ~/.local/bin must be on PATH through ENV, and before the
  installer's own `claude --version` check.
* THE HOOKS' TOOLS. Both PreToolUse hooks in .claude/settings.json pipe the command through jq
  into grep -P and end in `|| true`, so an image without either one runs every hook as an allow.
  common/tests/test_harness_hooks.py checks the tools wherever the suite runs; this pins that the
  image itself carries them, which CI, running in a different image, cannot otherwise see.
* NO APT NODE (tj-3mk3u5.52). nodejs and npm left the apt line with the npm install, and an unused
  package manager is supply-chain surface. Nothing would notice them slipping back in, so the pin
  makes adding them a decision instead. Node did come back, for the web chain (tj-grna9p.97), as the
  pinned release tarball below -- never as bookworm's apt packages, which are Node 18.
* THE BUF PIN (tj-3mk3u5.54). The image's buf RUN mirrors the Makefile's BUF_VERSION and both
  SHA-256s, which nothing else compares: the build context cannot read the Makefile. A drift is a
  checksum failure at the user's next rebuild, or an image whose buf `make lint` refuses.
* THE NODE PIN (tj-grna9p.97). The same shape: the image's node RUN mirrors the Makefile's
  NODE_VERSION, NODE_SHA256_X86_64 and NODE_SHA256_AARCH64. A drift is a checksum failure at the
  next rebuild, or an agent image whose Node differs from the one the root Dockerfile and CI build
  the web chain with -- the version /web is tested with is then not the one it ships with.
"""

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

from common.tests.roots import REPO_ROOT
from common.tests.test_ci_invariants import _expanded_make_variable


pytestmark = pytest.mark.build_infra

# THE TRUE REPOSITORY ROOT (tj-iontkq.2): .devcontainer/, .claude/ and the Makefile this module
# reads all stay at the top of the repository, so this is REPO_ROOT and never SERVER_ROOT.
DOCKERFILE = REPO_ROOT / '.devcontainer' / 'Dockerfile'
WORKFLOW_MANIFEST = REPO_ROOT / '.claude' / 'workflow.yml'
INSTALLER_URL = 'https://claude.ai/install.sh'
# `curl ... install.sh | bash -s <version>`: the native installer's version argument.
INSTALLER_VERSION = re.compile(re.escape(INSTALLER_URL) + r'\s*\|\s*bash\s+-s\s+(\S+)')
ROOT_USERS = {'root', '0'}
APT_FRONTENDS = {'apt-get', 'apt'}
# apt options whose value is the next word, so that value is never read as a package or subcommand.
APT_OPTIONS_WITH_A_VALUE = {'-o', '--option', '-c', '--config-file', '-t', '--target-release'}
# `NAME=value` before a command word: an environment assignment, not the command.
ASSIGNMENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*=')
# Removed with the npm install of Claude Code (tj-3mk3u5.52): nothing in the agent image runs them.
UNUSED_PACKAGES = {'nodejs', 'npm'}


def _instructions() -> list[tuple[str, str]]:
    """The Dockerfile as (INSTRUCTION, arguments) pairs: continuations joined, comment lines dropped.

    Comment text is excluded on purpose. The Dockerfile explains the old npm install in prose, and a
    check that read comments would fail on the explanation of the fix.
    """
    instructions: list[tuple[str, str]] = []
    pending = ''
    for raw in DOCKERFILE.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.endswith('\\'):
            pending += line[:-1] + ' '
            continue
        keyword, _, arguments = (pending + line).partition(' ')
        pending = ''
        instructions.append((keyword.upper(), arguments.strip()))
    assert not pending, f'{DOCKERFILE.name} ends inside a continued instruction: {pending!r}'
    return instructions


def _installer_index(instructions: list[tuple[str, str]]) -> int:
    found = [
        index
        for index, (keyword, arguments) in enumerate(instructions)
        if keyword == 'RUN' and INSTALLER_URL in arguments
    ]
    assert len(found) == 1, (
        f'expected exactly one RUN with the native installer ({INSTALLER_URL}) in '
        f'{DOCKERFILE.relative_to(REPO_ROOT)}, found {len(found)}. With none, the image has no Claude Code.'
    )
    return found[0]


def _user_at(instructions: list[tuple[str, str]], index: int) -> str:
    """The USER in effect at instruction `index`: the last USER before it, or root when there is none."""
    users = [arguments for keyword, arguments in instructions[:index] if keyword == 'USER']
    return users[-1] if users else 'root'


def _apt_install_packages(run: str) -> list[str]:
    """The packages that each `apt-get install` or `apt install` in one RUN's shell text names, in order.

    Tokenised the way the shell splits it: quotes are respected, and `&&`, `||`, `;` and `|` end a
    command. So a package named outside an install (a purge, an echo) does not count, and an install
    counts wherever it sits in the RUN. Version, architecture and release qualifiers are dropped:
    `npm=9.2.0~ds1-1`, `nodejs:amd64` and `nodejs/bookworm-backports` name npm and nodejs.
    """
    lexer = shlex.shlex(run, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ''
    commands: list[list[str]] = [[]]
    for token in lexer:
        if token and all(char in lexer.punctuation_chars for char in token):
            commands.append([])
        else:
            commands[-1].append(token)
    packages: list[str] = []
    for words in commands:
        while words and ASSIGNMENT.match(words[0]):
            words = words[1:]
        if not words or Path(words[0]).name not in APT_FRONTENDS:
            continue
        operands: list[str] = []
        takes_value = False
        for word in words[1:]:
            if takes_value:
                takes_value = False
            elif word in APT_OPTIONS_WITH_A_VALUE:
                takes_value = True
            elif not word.startswith('-'):
                operands.append(word)
        if operands[:1] == ['install']:
            packages.extend(re.split(r'[=:/]', operand, maxsplit=1)[0] for operand in operands[1:])
    return packages


def _apt_installs() -> list[tuple[str, str]]:
    """(package, the RUN that installs it) for every package any apt install in the Dockerfile names."""
    return [
        (package, arguments)
        for keyword, arguments in _instructions()
        if keyword == 'RUN'
        for package in _apt_install_packages(arguments)
    ]


def test_claude_code_installs_at_the_version_the_workflow_manifest_pins() -> None:
    """The installer's version argument is the one .claude/workflow.yml pins as claude_code.version."""
    instructions = _instructions()
    match = INSTALLER_VERSION.search(instructions[_installer_index(instructions)][1])
    assert match, f'the installer RUN passes no version argument (`| bash -s <version>`): {instructions}'
    manifest = yaml.safe_load(WORKFLOW_MANIFEST.read_text(encoding='utf-8'))
    pinned = str(((manifest.get('toolchain') or {}).get('claude_code') or {}).get('version') or '')
    assert pinned, f'{WORKFLOW_MANIFEST.relative_to(REPO_ROOT)} pins no toolchain.claude_code.version'
    assert match.group(1) == pinned, (
        f'the agent image installs Claude Code {match.group(1)}, but {WORKFLOW_MANIFEST.relative_to(REPO_ROOT)} '
        f'pins {pinned}. The Dockerfile names that pin as the one to bump; move both together.'
    )


def test_claude_code_installs_as_the_non_root_agent_user() -> None:
    """After USER, as a non-root user: otherwise the install is root-owned and cannot update itself."""
    instructions = _instructions()
    user = _user_at(instructions, _installer_index(instructions))
    assert user.split(':')[0] not in ROOT_USERS, (
        f'the native installer runs as {user!r}. Root owns what it installs, and the agent user cannot '
        f'update it in place -- the npm-global failure tj-gys6xn removed.'
    )


def test_the_agent_users_local_bin_is_on_path_before_the_installer_runs() -> None:
    """ENV PATH carries ~/.local/bin of the installing user, set before the installer's own check."""
    instructions = _instructions()
    installer = _installer_index(instructions)
    user = _user_at(instructions, installer).split(':')[0]
    local_bin = '/root/.local/bin' if user in ROOT_USERS else f'/home/{user}/.local/bin'
    on_path = [
        index
        for index, (keyword, arguments) in enumerate(instructions)
        if keyword == 'ENV' and re.match(r'PATH[=\s]', arguments) and local_bin in arguments
    ]
    assert on_path, (
        f'no ENV PATH puts {local_bin} on PATH. The entrypoint execs its command and no login shell '
        f"runs, so the installer's ~/.bashrc line is never read and `claude` does not resolve."
    )
    assert on_path[0] < installer, (
        f'{local_bin} reaches PATH only after the installer, whose `claude --version` needs it'
    )


def test_no_npm_install_of_claude_code_remains() -> None:
    """The root npm-global install is gone, not merely shadowed by the native one."""
    leftovers = [
        arguments
        for keyword, arguments in _instructions()
        if keyword == 'RUN' and '@anthropic-ai/claude-code' in arguments
    ]
    assert not leftovers, f'a RUN still installs the npm package as root: {leftovers}'


def test_the_agent_image_carries_the_hook_pipelines_tools() -> None:
    """Jq from apt, and a Debian base, whose essential GNU grep is built with -P.

    On a base whose grep has no -P (busybox, as on Alpine) both hooks would allow every command.
    """
    instructions = _instructions()
    bases = [arguments for keyword, arguments in instructions if keyword == 'FROM']
    assert bases and bases[-1].split()[0].startswith('debian:'), (
        f"the agent image is built FROM {bases}; the hooks need a grep with -P, which Debian's "
        f'essential GNU grep provides. On another base, install one and update this pin.'
    )
    apt_packages = {package for package, _ in _apt_installs()}
    assert 'jq' in apt_packages, (
        'no apt-get install in the agent image installs jq, which both PreToolUse hooks in '
        '.claude/settings.json pipe every command through; without it they allow everything.'
    )


def test_no_apt_install_brings_back_nodejs_or_npm() -> None:
    """The nodejs and npm packages left with the npm install of Claude Code (tj-3mk3u5.52), and stay out.

    Nothing in the image runs them: claude is the native binary, the hooks run jq and grep, the
    entrypoint is sh, .mcp.json's one server is http, and VS Code brings its own node. The
    Dockerfile's note names what would justify adding them back (something that runs them, such as
    an MCP server started through npx), and this pin makes that a decision rather than a word that
    slips back onto an apt line.
    """
    installs = _apt_installs()
    assert installs, f'found no apt install in {DOCKERFILE.relative_to(REPO_ROOT)}, so this pin would pass vacuously'
    returned = [(package, run) for package, run in installs if package in UNUSED_PACKAGES]
    assert not returned, (
        f'the agent image apt-installs {sorted({package for package, _ in returned})} again: '
        f'{[run for _, run in returned]}. tj-3mk3u5.52 removed them because nothing in the image runs '
        "them. If something now does, name it in the Dockerfile's note and change this pin in the same commit."
    )


@pytest.mark.parametrize(
    ('run', 'packages'),
    [
        pytest.param(
            'apt-get update && apt-get install -y --no-install-recommends jq nodejs && rm -rf /var/lib/apt/lists/*',
            ['jq', 'nodejs'],
            id='chained-install',
        ),
        pytest.param(
            'apt-get -y install npm=9.2.0~ds1-1 nodejs:amd64 nodejs/bookworm-backports',
            ['npm', 'nodejs', 'nodejs'],
            id='options-first-and-qualifiers',
        ),
        pytest.param(
            'DEBIAN_FRONTEND=noninteractive apt-get install -y -o Dpkg::Use-Pty=0 -t bookworm-backports npm',
            ['npm'],
            id='assignment-and-valued-options',
        ),
        pytest.param('apt install nodejs; /usr/bin/apt-get install npm', ['nodejs', 'npm'], id='apt-and-absolute-path'),
        pytest.param(
            'apt-get purge -y nodejs npm && echo "apt-get install nodejs" && rm -rf /var/lib/apt',
            [],
            id='not-an-install',
        ),
    ],
)
def test_the_apt_reader_finds_what_each_install_names(run: str, packages: list[str]) -> None:
    """The reader behind the apt pins. One that found nothing would pass every no-X pin vacuously."""
    assert _apt_install_packages(run) == packages


# ---------------------------------------------------------------------------------------------------
# THE BUF PIN'S ONE MIRROR (tj-3mk3u5.54). The Makefile is the one authority for BUF_VERSION and both
# SHA-256s. This build's context is .devcontainer/, so its RUN cannot read the Makefile and carries a
# copy, which nothing but this compares. The RUN is EXECUTED, under /bin/sh as docker runs it, with
# dpkg, curl, sha256sum, install, rm and buf stubbed first on PATH: what is pinned is what the shell
# does for each architecture -- the asset it fetches, the checksum it checks that file against, where
# it installs it -- not how the text is spelled. Nothing is downloaded, and nothing outside the test's
# directory is written. The arm64 branch is shown here only: no image is built for it anywhere.

MAKEFILE = REPO_ROOT / 'Makefile'
BUF_RELEASES = 'https://github.com/bufbuild/buf/releases/download'
BUF_DESTINATION = '/usr/local/bin/buf'
# One stand-in under every stubbed name. Each call is a log line: its name, then its arguments,
# US-separated. The buf and node RUNs share it; each stubs only the commands it calls.
RUN_STUB = r"""#!/bin/sh
name="${0##*/}"
{ printf '%s' "$name"; for word in "$@"; do printf '\037%s' "$word"; done; printf '\n'; } >> "$STUB_LOG"
case "$name" in
  dpkg) printf '%s\n' "$DPKG_ARCH" ;;
  sha256sum) cat >> "$SHA256SUM_STDIN"; exit "${SHA256SUM_EXIT:-0}" ;;
  buf) printf '%s\n' "$BUF_REPORTS" ;;
  node) printf '%s\n' "$NODE_REPORTS" ;;
  npm) printf '%s\n' '0.0.0-stub' ;;
esac
exit 0
"""
RUN_STUBBED = ('dpkg', 'curl', 'sha256sum', 'install', 'rm', 'buf')


def _buf_run_index(instructions: list[tuple[str, str]]) -> int:
    found = [
        i for i, (keyword, arguments) in enumerate(instructions) if keyword == 'RUN' and 'bufbuild/buf' in arguments
    ]
    assert len(found) == 1, f'expected exactly one RUN that installs buf in {DOCKERFILE.name}, found {len(found)}'
    return found[0]


def _make_pin(name: str) -> str:
    """The Makefile's value, as make expands it; an outer make's command-line variables kept out."""
    env = {key: value for key, value in os.environ.items() if key not in ('MAKEFLAGS', 'MFLAGS', 'MAKELEVEL')}
    return _expanded_make_variable(name, REPO_ROOT, env)


def _run_stubbed(
    run: str, stubbed: tuple[str, ...], tmp_path: Path, arch: str, **variables: str
) -> tuple[subprocess.CompletedProcess, list[list[str]], str]:
    """One RUN's shell text, executed with STUBBED first on PATH: (result, calls, sha256sum's stdin)."""
    stubs = tmp_path / 'bin'
    stubs.mkdir()
    for name in stubbed:
        (stubs / name).write_text(RUN_STUB, encoding='utf-8')
        (stubs / name).chmod(0o755)
    log, stdin = tmp_path / 'calls', tmp_path / 'sha256sum.stdin'
    env = {
        'PATH': f'{stubs}{os.pathsep}{os.environ.get("PATH", "")}',
        'STUB_LOG': str(log),
        'SHA256SUM_STDIN': str(stdin),
        'DPKG_ARCH': arch,
        **variables,
    }
    result = subprocess.run(['/bin/sh', '-c', run], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
    calls = [line.split('\x1f') for line in log.read_text(encoding='utf-8').splitlines()] if log.exists() else []
    return result, calls, stdin.read_text(encoding='utf-8') if stdin.exists() else ''


def _run_buf_install(
    tmp_path: Path, arch: str, **variables: str
) -> tuple[subprocess.CompletedProcess, list[list[str]], str]:
    """The image's buf RUN, executed with every command it calls stubbed: (result, calls, sha256sum's stdin)."""
    instructions = _instructions()
    run = instructions[_buf_run_index(instructions)][1]
    return _run_stubbed(run, RUN_STUBBED, tmp_path, arch, **{'BUF_REPORTS': _make_pin('BUF_VERSION'), **variables})


def test_buf_installs_as_root_into_usr_local_bin() -> None:
    """Before the USER switch: /usr/local/bin is root's to write, and the image build fails there otherwise."""
    instructions = _instructions()
    user = _user_at(instructions, _buf_run_index(instructions))
    assert user.split(':')[0] in ROOT_USERS, f'the buf RUN runs as {user!r}, who cannot install into /usr/local/bin'


@pytest.mark.parametrize(
    ('arch', 'asset', 'checksum'),
    [('amd64', 'buf-Linux-x86_64', 'BUF_SHA256_X86_64'), ('arm64', 'buf-Linux-aarch64', 'BUF_SHA256_AARCH64')],
)
def test_the_image_installs_the_makefiles_buf_pin_for_each_arch(
    tmp_path: Path, arch: str, asset: str, checksum: str
) -> None:
    """Gate 8: the Makefile's version and this arch's checksum are the ones the image fetches and checks.

    The asset is named by the Makefile's BUF_VERSION, the downloaded file is checked against the
    Makefile's checksum for this arch, and the file checked is the file installed, at 0755, then run.
    """
    result, calls, stdin = _run_buf_install(tmp_path, arch)
    assert result.returncode == 0, f'{result.stdout}\n{result.stderr}'
    assert [call[0] for call in calls] == ['dpkg', 'curl', 'sha256sum', 'install', 'rm', 'buf', 'buf'], calls
    curl, sha256sum, install = calls[1][1:], calls[2][1:], calls[3][1:]
    assert curl[-1] == f'{BUF_RELEASES}/v{_make_pin("BUF_VERSION")}/{asset}', curl
    downloaded = curl[curl.index('-o') + 1]
    assert '-c' in sha256sum and stdin == f'{_make_pin(checksum)}  {downloaded}\n', (sha256sum, stdin)
    assert install[-2:] == [downloaded, BUF_DESTINATION] and install[install.index('-m') + 1] == '0755', install


@pytest.mark.parametrize(
    ('arch', 'variables', 'reached', 'says'),
    [
        ('riscv64', {}, ['dpkg'], 'no buf checksum pinned for riscv64'),
        ('amd64', {'SHA256SUM_EXIT': '1'}, ['dpkg', 'curl', 'sha256sum'], ''),
        ('amd64', {'BUF_REPORTS': '1.0.0'}, ['dpkg', 'curl', 'sha256sum', 'install', 'rm', 'buf', 'buf'], ''),
    ],
    ids=['an-unpinned-arch', 'a-checksum-mismatch', 'another-version'],
)
def test_the_image_build_fails_on_an_unpinned_arch_a_mismatch_or_another_version(
    tmp_path: Path, arch: str, variables: dict[str, str], reached: list[str], says: str
) -> None:
    """Each refusal stops the build: nothing is fetched for an unpinned arch, nothing installed on a mismatch."""
    result, calls, _ = _run_buf_install(tmp_path, arch, **variables)
    assert result.returncode != 0, f'{result.stdout}\n{result.stderr}'
    assert [call[0] for call in calls] == reached, calls
    assert says in result.stderr


# ---------------------------------------------------------------------------------------------------
# THE NODE PIN'S ONE MIRROR (tj-grna9p.97). The same rule as buf's: the Makefile's NODE_VERSION and
# both NODE_SHA256_* are the authority, the image's node RUN carries a copy, and that RUN is executed
# under /bin/sh with every command it calls stubbed. The tarball unpacks into /usr/local, so `node`
# and `npm` land on the default PATH; the final check compares `node --version` with the pin.

NODE_RELEASES = 'https://nodejs.org/dist'
NODE_DESTINATION = '/usr/local'
NODE_RUN_STUBBED = ('dpkg', 'curl', 'sha256sum', 'tar', 'rm', 'node', 'npm')
NODE_CALLS = ['dpkg', 'curl', 'sha256sum', 'tar', 'rm', 'node', 'npm', 'node']


def _node_run_index(instructions: list[tuple[str, str]]) -> int:
    found = [
        i for i, (keyword, arguments) in enumerate(instructions) if keyword == 'RUN' and NODE_RELEASES in arguments
    ]
    assert len(found) == 1, f'expected exactly one RUN that installs node in {DOCKERFILE.name}, found {len(found)}'
    return found[0]


def _run_node_install(
    tmp_path: Path, arch: str, **variables: str
) -> tuple[subprocess.CompletedProcess, list[list[str]], str]:
    """The image's node RUN, executed with every command it calls stubbed: (result, calls, sha256sum's stdin)."""
    instructions = _instructions()
    run = instructions[_node_run_index(instructions)][1]
    reports = {'NODE_REPORTS': f'v{_make_pin("NODE_VERSION")}', **variables}
    return _run_stubbed(run, NODE_RUN_STUBBED, tmp_path, arch, **reports)


def test_node_installs_as_root_into_usr_local() -> None:
    """Before the USER switch: /usr/local is root's to write, and the image build fails there otherwise."""
    instructions = _instructions()
    user = _user_at(instructions, _node_run_index(instructions))
    assert user.split(':')[0] in ROOT_USERS, f'the node RUN runs as {user!r}, who cannot unpack into /usr/local'


@pytest.mark.parametrize(
    ('arch', 'node_arch', 'checksum'),
    [('amd64', 'x64', 'NODE_SHA256_X86_64'), ('arm64', 'arm64', 'NODE_SHA256_AARCH64')],
)
def test_the_image_installs_the_makefiles_node_pin_for_each_arch(
    tmp_path: Path, arch: str, node_arch: str, checksum: str
) -> None:
    """The Makefile's NODE_VERSION and this arch's checksum are the ones the image fetches and checks.

    The release tarball is named by the Makefile's version, the downloaded file is checked against
    the Makefile's checksum for this arch, and the file checked is the file unpacked into /usr/local.
    """
    version = _make_pin('NODE_VERSION')
    result, calls, stdin = _run_node_install(tmp_path, arch)
    assert result.returncode == 0, f'{result.stdout}\n{result.stderr}'
    assert [call[0] for call in calls] == NODE_CALLS, calls
    curl, tar = calls[1][1:], calls[3][1:]
    assert curl[-1] == f'{NODE_RELEASES}/v{version}/node-v{version}-linux-{node_arch}.tar.gz', curl
    downloaded = curl[curl.index('-o') + 1]
    assert stdin == f'{_make_pin(checksum)}  {downloaded}\n', stdin
    assert '-xzf' in tar and tar[tar.index('-xzf') + 1] == downloaded, tar
    assert tar[tar.index('-C') + 1] == NODE_DESTINATION, tar


@pytest.mark.parametrize(
    ('arch', 'variables', 'reached', 'says'),
    [
        ('riscv64', {}, ['dpkg'], 'no node checksum pinned for riscv64'),
        ('amd64', {'SHA256SUM_EXIT': '1'}, ['dpkg', 'curl', 'sha256sum'], ''),
        ('amd64', {'NODE_REPORTS': 'v1.0.0'}, NODE_CALLS, ''),
    ],
    ids=['an-unpinned-arch', 'a-checksum-mismatch', 'another-version'],
)
def test_the_node_install_fails_on_an_unpinned_arch_a_mismatch_or_another_version(
    tmp_path: Path, arch: str, variables: dict[str, str], reached: list[str], says: str
) -> None:
    """Each refusal stops the build: nothing fetched for an unpinned arch, nothing unpacked on a mismatch."""
    result, calls, _ = _run_node_install(tmp_path, arch, **variables)
    assert result.returncode != 0, f'{result.stdout}\n{result.stderr}'
    assert [call[0] for call in calls] == reached, calls
    assert says in result.stderr
