"""build_infra pins for the agent image, .devcontainer/Dockerfile (tj-qenrpk; commit 62a15e0, bead tj-gys6xn).

The image installs Claude Code with the native installer, run as the agent user after the USER
switch, so the binary lands in ~/.local/bin, owned by the user who runs it, and Claude can update
itself in place. Before 62a15e0 it was a root `npm install -g`, which the updater could not write
to. An earlier attempt declared the dev container feature instead, which a plain `docker compose
build` never applies, and the image came out with no Claude at all -- silently (tj-gys6xn's notes).
Nothing can build the image here (the agent container has no Docker), so these read the Dockerfile.

Four properties, each one a silent failure if lost:

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
"""

import re
from pathlib import Path

import pytest
import yaml


pytestmark = pytest.mark.build_infra

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / '.devcontainer' / 'Dockerfile'
WORKFLOW_MANIFEST = REPO_ROOT / '.claude' / 'workflow.yml'
INSTALLER_URL = 'https://claude.ai/install.sh'
# `curl ... install.sh | bash -s <version>`: the native installer's version argument.
INSTALLER_VERSION = re.compile(re.escape(INSTALLER_URL) + r'\s*\|\s*bash\s+-s\s+(\S+)')
ROOT_USERS = {'root', '0'}


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
    apt_packages = {
        word
        for keyword, arguments in instructions
        if keyword == 'RUN' and 'apt-get install' in arguments
        for word in arguments.split()
    }
    assert 'jq' in apt_packages, (
        'no apt-get install in the agent image installs jq, which both PreToolUse hooks in '
        '.claude/settings.json pipe every command through; without it they allow everything.'
    )
