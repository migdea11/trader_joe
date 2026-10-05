"""The agent-stack MCP's host lifecycle, RUN: make targets and the devcontainer's initializeCommand, docker stubbed.

tj-c4mosr.5: 01:43 (a)-(d) (rulings since addendum 7), 04:42 A1-A5 as amended by 04:34 and P8, 04:34
M4, the validator's P3 and P5-P9, and the architect's (a), (c) and (d) of 05:41 in their 06:28 final
shape. Design: ADR tj-4rr0la addenda 4, 7, 9, 11 and 12.

Docker-free: `docker` (and, for the initializeCommand, `make`) are stub scripts first on PATH that log
every call and answer from environment variables, so each branch of each recipe runs for real. make
reports a failed recipe as exit 2 -- 'refuses, exit 1' in the design means exactly that (addendum 12
F6) -- so refusals are asserted as 2, never 1. What only a daemon or an IDE can show (a real start
does not rebuild, a real rebuild does, the devcontainer reaches the MCP) is tj-c4mosr.6 H0.
"""

import functools
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from common.tests.compose_model import AGENT_MCP_FILE, load
from common.tests.test_ci_invariants import (
    MAKEFILE,
    REPO_ROOT,
    _expanded_make_variable,
    _make_recipe,
    _make_variable,
    _subprocess_env,
)
from common.tests.test_network_model import _make_prerequisites


pytestmark = pytest.mark.build_infra

MCP_PROJECT = 'trader_joe_agent_mcp'


@functools.cache
def _agent_compose_call() -> str:
    """`$(AGENT_COMPOSE)` as the docker stub logs it, taken from make rather than respelled here.

    AGENT_COMPOSE carries --env-file since tj-ywpuxx, and its value depends on the checkout: the
    absolute path of the root .env when there is one, /dev/null when there is not. Spelling one of
    them here would make these assertions pass or fail by whether the machine running them happens
    to have a .env. What the flag is FOR is pinned on .devcontainer/compose.yml, not here.
    """
    return _expanded_make_variable('AGENT_COMPOSE', REPO_ROOT, _subprocess_env()).removeprefix('docker ')


PLAIN_START_TARGETS = ('agent-mcp-up', 'agent-up')
ADDENDUM_7 = 'the user ruling of ADR tj-4rr0la addendum 7'

DOCKER_STUB = r"""#!/bin/sh
printf 'docker %s\n' "$*" >> "$STUB_LOG"
case "$*" in
  "network inspect -f {{.Internal}} trader_joe_agent_mcp") echo "${STUB_INTERNAL:-true}" ;;
  "network inspect "*) exit "${STUB_NET_INSPECT:-0}" ;;
  "network create "*) exit "${STUB_NET_CREATE:-0}" ;;
  *"service=socket_proxy"*) [ -n "${STUB_PROXY_IDS-proxy1}" ] && printf '%s\n' ${STUB_PROXY_IDS-proxy1} ;;
  *"service=agent_mcp"*) [ -n "${STUB_MCP_IDS-mcp1}" ] && printf '%s\n' ${STUB_MCP_IDS-mcp1} ;;
  "inspect -f {{.State.Health.Status}} "*) echo healthy ;;
  "restart "*) exit "${STUB_RESTART_EXIT:-0}" ;;
  *"docker-compose.agent-mcp.yaml"*) exit "${STUB_MCP_COMPOSE_EXIT:-0}" ;;
  *".devcontainer/compose.yml build"*) exit "${STUB_AGENT_BUILD_EXIT:-0}" ;;
esac
exit 0
"""

MAKE_STUB = r"""#!/bin/sh
printf 'make %s\n' "$*" >> "$STUB_LOG"
exit "${STUB_MAKE_EXIT:-0}"
"""


@dataclass
class Host:
    """A tmp host: stub directory, log, and the four host paths the MCP targets read."""

    root: Path
    stubs: Path
    log: Path
    repo: Path
    home: Path
    share: Path
    stack_dir: Path

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def docker_calls(self) -> list[str]:
        return [call.removeprefix('docker ') for call in self.calls() if call.startswith('docker ')]


@pytest.fixture
def host(tmp_path: Path) -> Host:
    root = tmp_path.resolve()
    stubs = root / 'stubs'
    stubs.mkdir()
    docker = stubs / 'docker'
    docker.write_text(DOCKER_STUB)
    docker.chmod(0o755)
    repo = root / 'repo'
    repo.mkdir()
    return Host(
        root=root,
        stubs=stubs,
        log=root / 'calls.log',
        repo=repo,
        home=root / 'agent_home',
        share=root / 'share',
        stack_dir=root / 'stack',
    )


def _env(host: Host, **overrides: str | None) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in (
            'MAKEFLAGS',
            'MFLAGS',
            'MAKELEVEL',
            'AGENT_MCP',
            'XDG_RUNTIME_DIR',
            'AGENT_MCP_SHARE_PATH',
            'PYTEST_ADDOPTS',
        )
    }
    env.update(PATH=f'{host.stubs}:{os.environ["PATH"]}', HOME=str(host.root), STUB_LOG=str(host.log))
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _make(host: Host, *arguments: str, paths: bool = True, **env: str | None) -> subprocess.CompletedProcess:
    # From the repository root, as a user runs it: agent-up and agent-build re-enter make with a bare
    # $(MAKE), which reads the Makefile in the current directory.
    command = ['make', '--no-print-directory', '-f', str(MAKEFILE), '-C', str(REPO_ROOT), *arguments]
    if paths:
        command += [
            f'AGENT_MCP_REPO_HOST_PATH={host.repo}',
            f'AGENT_HOME_PATH={host.home}',
            f'AGENT_MCP_SHARE_PATH={host.share}',
            f'AGENT_MCP_STACK_DIR={host.stack_dir}',
        ]
    return subprocess.run(command, capture_output=True, text=True, env=_env(host, **env), check=False, timeout=120)


def _dry_run(host: Host, target: str, **env: str | None) -> str:
    result = _make(host, '-n', target, **env)
    assert result.returncode == 0, result.stderr
    return result.stdout


# --- (a), (c), P5: the default start and the opt-out ---------------------------------------------

_MCP_START_MARKER = "docker network inspect -f '{{.Internal}}' trader_joe_agent_mcp"


@pytest.mark.parametrize('value', [None, '', 'on', 'OFF', 'no'], ids=['unset', 'empty', 'on', 'OFF', 'no'])
def test_agent_up_starts_the_mcp_unless_agent_mcp_is_exactly_off(host: Host, value: str | None):
    """(a): a dry run of agent-up includes agent-mcp-up's recipe when AGENT_MCP is unset or anything but 'off'."""
    printed = _dry_run(host, 'agent-up', AGENT_MCP=value)
    assert _MCP_START_MARKER in printed, f'agent-up does not start the MCP with AGENT_MCP={value!r} ({ADDENDUM_7})'


def test_agent_up_with_agent_mcp_off_skips_the_mcp_but_not_its_network_or_share(host: Host):
    """(a) and P5: the opt-out skips agent-mcp-up; the devcontainer still needs the network and the share."""
    printed = _dry_run(host, 'agent-up', AGENT_MCP='off')
    assert _MCP_START_MARKER not in printed and 'agent-mcp-up' in printed, (
        f'AGENT_MCP=off must skip the MCP start on the make path ({ADDENDUM_7})'
    )
    prerequisites = _make_prerequisites('agent-up')
    assert {'dev-network', 'agent-mcp-network', 'agent-mcp-share'} <= set(prerequisites), prerequisites
    assert 'agent-mcp-up' not in prerequisites, 'a prerequisite that fails stops make; the MCP start must warn instead'


def test_a_failed_mcp_start_warns_and_the_devcontainer_still_starts(host: Host):
    """(a)/addendum 7 (2) on the make path: agent-up exits 0, warns naming both remedies, runs compose up."""
    result = _make(host, 'agent-up', STUB_INTERNAL='false')
    assert result.returncode == 0, result.stderr
    assert 'make agent-mcp-up' in result.stderr and 'AGENT_MCP=off' in result.stderr, result.stderr
    assert f'{_agent_compose_call()} up -d' in host.docker_calls(), host.docker_calls()


def test_agent_down_never_stops_the_mcp(host: Host):
    """(c): the MCP outlives the devcontainer; agent-down stops the devcontainer and nothing else."""
    closure = _prerequisite_closure('agent-down')
    lines = ' '.join(line for target in closure for line in _recipe_or_empty(target))
    assert 'agent-mcp-down' not in closure and 'agent-mcp-down' not in lines, (
        f'agent-down reaches agent-mcp-down ({ADDENDUM_7})'
    )
    result = _make(host, 'agent-down')
    assert result.returncode == 0, result.stderr
    assert host.docker_calls() == [f'{_agent_compose_call()} down'], (
        f'agent-down ran {host.docker_calls()}; it must not stop the MCP ({ADDENDUM_7})'
    )


# --- (b): no venv on the host path -------------------------------------------------------------


def _recipe_or_empty(target: str) -> list[str]:
    try:
        return _make_recipe(target)
    except AssertionError:
        return []


def _prerequisite_closure(target: str) -> list[str]:
    seen, queue = [], [target]
    while queue:
        name = queue.pop(0)
        if name in seen:
            continue
        seen.append(name)
        if re.match(r'^[\w-]+$', name):
            queue += _make_prerequisites(name)
    return seen


@pytest.mark.parametrize(
    'target', ['agent-mcp-up', 'agent-mcp-rebuild', 'agent-mcp-down', 'agent-mcp-paths', 'agent-mcp-share']
)
def test_the_mcp_targets_need_no_venv_and_run_no_uv(target: str):
    """(b): they run on a bare host -- the IDE's initializeCommand calls agent-mcp-up on every start."""
    closure = _prerequisite_closure(target)
    assert not {'$(VENV_MARKER)', '$(VENV_PYTHON)'} & set(closure), f'{target} needs the venv: {closure}'
    for name in closure:
        for line in _recipe_or_empty(name):
            assert not re.search(r'(^|[\s;&|])uv\s', line), f'{name} runs uv: {line}'


# --- P3: the share directory's check, in both places -----------------------------------------------


def _initialize_command() -> str:
    lines = (REPO_ROOT / '.devcontainer' / 'devcontainer.json').read_text(encoding='utf-8').splitlines()
    return json.loads('\n'.join(line for line in lines if not line.lstrip().startswith('//')))['initializeCommand']


def _run_initialize(host: Host, **env: str | None) -> subprocess.CompletedProcess:
    make_stub = host.stubs / 'make'
    if not make_stub.exists():
        make_stub.write_text(MAKE_STUB)
        make_stub.chmod(0o755)
    kit = host.root / 'kit'
    (kit / 'skills').mkdir(parents=True, exist_ok=True)
    command = _initialize_command().replace('${localWorkspaceFolder}', str(REPO_ROOT))
    base = {'AGENT_KIT_PATH': str(kit), 'AGENT_HOME_PATH': str(host.home), 'PATH': f'{host.stubs}:/usr/bin:/bin'}
    return subprocess.run(
        ['/bin/sh', '-c', command],
        capture_output=True,
        text=True,
        env=_env(host, **{**base, **env}),
        check=False,
        timeout=60,
    )


def _share_via_make(host: Host, share: str) -> subprocess.CompletedProcess:
    return _make(host, 'agent-mcp-share', f'AGENT_MCP_SHARE_PATH={share}', paths=False)


def _share_via_initialize(host: Host, share: str) -> subprocess.CompletedProcess:
    return _run_initialize(host, AGENT_MCP_SHARE_PATH=share, XDG_RUNTIME_DIR=str(host.root / 'xdg'))


SHARE_RUNNERS = {'make agent-mcp-share': _share_via_make, 'initializeCommand': _share_via_initialize}


def _prepare(host: Host, case: str) -> str:
    share = host.root / 'share'
    if case == 'mode 755':
        share.mkdir(mode=0o755)
        share.chmod(0o755)
    elif case == 'mode 2700':
        share.mkdir(mode=0o700)
        share.chmod(0o2700)
    elif case == 'symlink to our own 0700 dir':
        own = host.root / 'own'
        own.mkdir(mode=0o700)
        share.symlink_to(own, target_is_directory=True)
    elif case == 'regular file':
        share.write_text('x')
    elif case == 'root-owned /tmp':
        return '/tmp'
    elif case == 'relative path':
        return 'relative/share'
    return str(share)


_SHARE_REFUSALS = [
    'mode 755',
    'mode 2700',
    'symlink to our own 0700 dir',
    'regular file',
    'root-owned /tmp',
    'relative path',
]


@pytest.mark.parametrize('runner', list(SHARE_RUNNERS), ids=list(SHARE_RUNNERS))
@pytest.mark.parametrize('case', _SHARE_REFUSALS)
def test_the_share_check_refuses_a_directory_it_cannot_trust(host: Host, runner: str, case: str):
    """P3: refused with a non-zero exit (make's 2), in BOTH places the share is created."""
    share = _prepare(host, case)
    result = SHARE_RUNNERS[runner](host, share)
    assert result.returncode != 0, f'{runner} accepted a share directory that is a {case}: {result.stderr}'
    if runner == 'make agent-mcp-share':
        assert result.returncode == 2
    else:
        assert not [call for call in host.calls() if call.startswith(('docker', 'make'))], (
            'the start went on past a refusal'
        )
    if case == 'symlink to our own 0700 dir':
        assert (host.root / 'share').is_symlink()


@pytest.mark.parametrize('runner', list(SHARE_RUNNERS), ids=list(SHARE_RUNNERS))
@pytest.mark.parametrize('nesting', ['share', 'missing_parent/share'])
def test_the_share_check_creates_a_missing_directory_0700(host: Host, runner: str, nesting: str):
    share = host.root / nesting
    result = SHARE_RUNNERS[runner](host, str(share))
    assert result.returncode == 0, result.stderr
    assert share.is_dir() and not share.is_symlink() and (share.stat().st_mode & 0o7777) == 0o700
    again = SHARE_RUNNERS[runner](host, str(share))
    assert again.returncode == 0, 'an existing, correct share directory must be accepted'


@pytest.mark.parametrize('xdg', [None, ''], ids=['xdg-unset', 'xdg-empty'])
def test_the_initialize_command_refuses_with_no_share_and_no_runtime_dir(host: Host, xdg: str | None):
    """P3 / addendum 12 F3: compose.yml has no uid for the Makefile's /tmp fallback, so the start refuses."""
    result = _run_initialize(host, AGENT_MCP_SHARE_PATH=None, XDG_RUNTIME_DIR=xdg)
    assert result.returncode != 0 and 'AGENT_MCP_SHARE_PATH' in result.stderr, result.stderr
    assert not host.docker_calls()


def test_the_initialize_command_defaults_the_share_under_the_runtime_dir(host: Host):
    """P4: $XDG_RUNTIME_DIR/trader_joe_agent_mcp, as compose.yml and the Makefile spell it."""
    xdg = host.root / 'xdg'
    xdg.mkdir()
    result = _run_initialize(host, AGENT_MCP_SHARE_PATH=None, XDG_RUNTIME_DIR=str(xdg))
    assert result.returncode == 0, result.stderr
    assert (xdg / 'trader_joe_agent_mcp').is_dir()


@pytest.mark.parametrize(
    ('xdg', 'expected'),
    [
        ('/run/user/4242', '/run/user/4242/trader_joe_agent_mcp'),
        (None, '/tmp/trader_joe_agent_mcp-{uid}'),
        ('', '/tmp/trader_joe_agent_mcp-{uid}'),
    ],
    ids=['xdg-set', 'xdg-unset', 'xdg-empty'],
)
def test_the_makefile_share_default(host: Host, xdg: str | None, expected: str):
    """P4, run for real: $(XDG_RUNTIME_DIR)/trader_joe_agent_mcp, else /tmp/trader_joe_agent_mcp-<uid>."""
    result = _make(host, '-s', '--eval', 'pv: ; @echo $(AGENT_MCP_SHARE_PATH)', 'pv', paths=False, XDG_RUNTIME_DIR=xdg)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected.format(uid=os.getuid())


# --- (d), P9: the initializeCommand's MCP step and its networks ------------------------------------


def test_the_initialize_command_warns_and_continues_when_the_mcp_start_fails(host: Host):
    """(d), ruled by addendum 7 (2): the MCP step cannot make the devcontainer start fail."""
    xdg = host.root / 'xdg'
    xdg.mkdir()
    result = _run_initialize(host, AGENT_MCP_SHARE_PATH=None, XDG_RUNTIME_DIR=str(xdg), STUB_MAKE_EXIT='1')
    assert result.returncode == 0, f'a failed MCP start failed the devcontainer start ({ADDENDUM_7}): {result.stderr}'
    assert 'WARNING' in result.stderr and 'make agent-mcp-up' in result.stderr and 'AGENT_MCP=off' in result.stderr
    makes = [call for call in host.calls() if call.startswith('make ')]
    assert makes == [f'make -C {REPO_ROOT} --no-print-directory agent-mcp-up'], makes


@pytest.mark.parametrize(('value', 'starts'), [('off', False), (None, True), ('on', True)], ids=['off', 'unset', 'on'])
def test_the_initialize_command_honours_the_opt_out(host: Host, value: str | None, starts: bool):
    xdg = host.root / 'xdg'
    xdg.mkdir()
    result = _run_initialize(host, AGENT_MCP_SHARE_PATH=None, XDG_RUNTIME_DIR=str(xdg), AGENT_MCP=value)
    assert result.returncode == 0, result.stderr
    makes = [call for call in host.calls() if call.startswith('make ')]
    assert bool(makes) is starts, f'AGENT_MCP={value!r}: make calls {makes} ({ADDENDUM_7})'


@pytest.mark.parametrize('network', ['trader_joe_devnet', 'trader_joe_agent_mcp'])
def test_the_initialize_command_fails_when_a_network_cannot_be_created(host: Host, network: str):
    """P9: both creates end '|| exit 1' -- compose.yml could not join a missing network anyway."""
    xdg = host.root / 'xdg'
    xdg.mkdir()
    stub = host.stubs / 'docker'
    stub.write_text(
        stub.read_text().replace(
            'case "$*" in', f'case "$*" in\n  "network inspect {network}"|"network create "*"{network}") exit 1 ;;', 1
        )
    )
    result = _run_initialize(host, AGENT_MCP_SHARE_PATH=None, XDG_RUNTIME_DIR=str(xdg))
    assert result.returncode != 0, f'the start went on without {network}'
    assert not [call for call in host.calls() if call.startswith('make ')]


def test_the_initialize_command_creates_devnet_ordinary_and_the_mcp_network_internal(host: Host):
    """P9: devnet is an ordinary bridge; trader_joe_agent_mcp is created --internal."""
    xdg = host.root / 'xdg'
    xdg.mkdir()
    stub = host.stubs / 'docker'
    stub.write_text(
        stub.read_text().replace('"network inspect "*) exit "${STUB_NET_INSPECT:-0}"', '"network inspect "*) exit 1')
    )
    result = _run_initialize(host, AGENT_MCP_SHARE_PATH=None, XDG_RUNTIME_DIR=str(xdg))
    assert result.returncode == 0, result.stderr
    creates = [call for call in host.docker_calls() if call.startswith('network create')]
    assert creates == [
        'network create --driver bridge trader_joe_devnet',
        'network create --driver bridge --internal trader_joe_agent_mcp',
    ]


# --- P8 / A1: a plain start never builds, rebuilds or recreates ---------------------------------------

_REBUILDING = re.compile(r'--build\b|\bbuild\b|--force-recreate|agent-mcp-rebuild|\bgit\s+archive\b')


def _unquoted(line: str) -> str:
    """A shell line with its double-quoted strings (messages) removed."""
    return re.sub(r'"[^"]*"', '""', line)


@pytest.mark.parametrize('target', PLAIN_START_TARGETS)
def test_no_plain_start_path_builds_or_recreates(host: Host, target: str):
    """P8 / (a) and A1, addendum 9: agent-mcp-up and agent-up reach no build, rebuild or recreate."""
    printed = _dry_run(host, target)
    assert _MCP_START_MARKER in printed, f'the dry run of {target} never reached the MCP start'
    offenders = [line for line in printed.splitlines() if _REBUILDING.search(_unquoted(line))]
    assert not offenders, f'a plain {target} builds or recreates the MCP: {offenders}'


def test_the_initialize_command_starts_and_never_rebuilds():
    command = _initialize_command()
    assert 'agent-mcp-up' in command and 'agent-mcp-rebuild' not in command and '--build' not in command


def test_only_agent_build_and_the_rebuild_target_itself_reach_a_rebuild():
    """P8 confirmed by the architect (05:41 (a)): agent-build is the only other target that does."""
    text = MAKEFILE.read_text(encoding='utf-8').replace('\\\n', ' ')
    reaching = set()
    for match in re.finditer(r'^([\w-]+)\s*:(?!=)([^\n]*)\n((?:\t[^\n]*\n?)*)', text, re.MULTILINE):
        target, prerequisites, recipe = match.groups()
        recipe = _unquoted(recipe)
        if 'agent-mcp-rebuild' in prerequisites.split('##')[0].split() or 'agent-mcp-rebuild' in recipe:
            reaching.add(target)
        if re.search(r'\$\(AGENT_MCP_COMPOSE\)[^\n]*--build', recipe):
            reaching.add(target)
    assert reaching == {'agent-build', 'agent-mcp-rebuild'}, reaching


def _mcp_compose_calls(host: Host) -> list[str]:
    return [
        call for call in host.docker_calls() if call.startswith('compose') and 'docker-compose.agent-mcp.yaml' in call
    ]


def _compose_prefix(host: Host) -> str:
    return (
        f'compose -p {MCP_PROJECT} --project-directory {host.repo} --env-file /dev/null '
        f'-f {host.repo}/docker-compose.agent-mcp.yaml'
    )


@pytest.mark.parametrize(
    ('proxy', 'mcp'), [('', ''), ('proxy1', ''), ('', 'mcp1')], ids=['none', 'mcp-missing', 'proxy-missing']
)
def test_the_create_branch_creates_only_what_is_missing_and_never_recreates(host: Host, proxy: str, mcp: str):
    """A1 amended (addendum 11 R3 change 1): `up --no-recreate`, from the existing image, no build."""
    result = _make(host, 'agent-mcp-up', STUB_PROXY_IDS=proxy, STUB_MCP_IDS=mcp)
    assert result.returncode == 0, result.stderr
    assert _mcp_compose_calls(host) == [f'{_compose_prefix(host)} up -d --wait --wait-timeout 120 --no-recreate']
    assert not [call for call in host.docker_calls() if call.startswith(('start', 'restart'))]


@pytest.mark.parametrize(('proxy', 'mcp'), [('p1 p2', 'mcp1'), ('proxy1', 'm1 m2')], ids=['two-proxies', 'two-mcps'])
def test_more_than_one_container_per_service_is_refused_starting_nothing(host: Host, proxy: str, mcp: str):
    """A1 amended (change 2): exactly one per service; two or more refuse, naming make agent-mcp-rebuild."""
    result = _make(host, 'agent-mcp-up', STUB_PROXY_IDS=proxy, STUB_MCP_IDS=mcp)
    assert result.returncode == 2 and 'make agent-mcp-rebuild' in result.stderr, result.stderr
    assert not [call for call in host.docker_calls() if call.startswith(('start', 'restart', 'compose'))]


def test_the_label_lookup_filters_project_service_and_one_offs(host: Host):
    _make(host, 'agent-mcp-up')
    lookups = [call for call in host.docker_calls() if call.startswith('ps ')]
    assert sorted(lookups) == sorted(
        f'ps -aq --filter label=com.docker.compose.project={MCP_PROJECT} --filter label=com.docker.compose.service={service} '
        f'--filter label=com.docker.compose.oneoff=False'
        for service in ('socket_proxy', 'agent_mcp')
    ), lookups


def test_one_container_each_is_started_by_docker_start_reading_no_compose_file(host: Host):
    """P7 and addendum 9 (a): the one-each branch has no compose call at all."""
    host.share.mkdir(mode=0o700)
    (host.share / 'agent_mcp_token').write_text('t' * 43)
    result = _make(host, 'agent-mcp-up')
    assert result.returncode == 0, result.stderr
    assert 'start proxy1 mcp1' in host.docker_calls()
    assert not [call for call in host.docker_calls() if call.startswith('compose')], host.docker_calls()


# --- (c) final shape: the token-missing branch ------------------------------------------------------

_WIPED = 'no token in'
_RESTART_DEVCONTAINER = 'a devcontainer that is already running keeps the old share directory'


def _stderr_lines(result: subprocess.CompletedProcess) -> list[str]:
    return result.stderr.splitlines()


def test_a_missing_token_restarts_agent_mcp_and_then_tells_the_user_about_the_devcontainer(host: Host):
    """(c): both lines on stderr in order, none on stdout, and the only restart is agent_mcp's."""
    result = _make(host, 'agent-mcp-up')
    assert result.returncode == 0, result.stderr
    lines = _stderr_lines(result)
    wiped = [index for index, line in enumerate(lines) if _WIPED in line]
    told = [index for index, line in enumerate(lines) if _RESTART_DEVCONTAINER in line]
    assert len(wiped) == 1 and len(told) == 1 and wiped[0] < told[0], lines
    assert 'make agent-down && make agent-up' in lines[told[0]]
    assert _WIPED not in result.stdout and _RESTART_DEVCONTAINER not in result.stdout
    restarts = [call for call in host.docker_calls() if call.startswith('restart')]
    assert restarts == ['restart mcp1'], restarts
    assert not [call for call in host.docker_calls() if 'devcontainer' in call or 'trader_joe_agent-' in call]


def test_a_present_token_prints_neither_line_and_restarts_nothing(host: Host):
    host.share.mkdir(mode=0o700)
    (host.share / 'agent_mcp_token').write_text('t' * 43)
    result = _make(host, 'agent-mcp-up')
    assert result.returncode == 0, result.stderr
    assert _WIPED not in result.stderr and _RESTART_DEVCONTAINER not in result.stderr
    assert not [call for call in host.docker_calls() if call.startswith('restart')]


def test_a_failed_restart_fails_the_recipe_and_promises_nothing(host: Host):
    """(c) and (d): make's 2; the devcontainer line promises a working MCP, so it must not print."""
    result = _make(host, 'agent-mcp-up', STUB_RESTART_EXIT='1')
    assert result.returncode == 2, (result.returncode, result.stderr)
    assert _WIPED in result.stderr and _RESTART_DEVCONTAINER not in result.stderr


def test_a_non_internal_mcp_network_is_refused(host: Host):
    result = _make(host, 'agent-mcp-up', STUB_INTERNAL='false')
    assert result.returncode == 2 and 'is not internal' in result.stderr
    assert not [call for call in host.docker_calls() if call.startswith(('ps', 'start', 'compose'))]


# --- A2, A3, A5, P6, P7: the rebuild, the stop, the source ---------------------------------------


def test_the_forced_rebuild_builds_recreates_and_removes_orphans(host: Host):
    """A2 amended (addendum 11 R3 change 3)."""
    result = _make(host, 'agent-mcp-rebuild')
    assert result.returncode == 0, result.stderr
    assert _mcp_compose_calls(host) == [
        f'{_compose_prefix(host)} up -d --wait --wait-timeout 120 --build --force-recreate --remove-orphans'
    ]


def test_agent_mcp_down_stops_and_keeps_the_containers(host: Host):
    """A3, addendum 9 (a): `docker stop` by label; never down, rm or a compose file."""
    result = _make(host, 'agent-mcp-down', paths=False)
    assert result.returncode == 0, result.stderr
    acting = [call for call in host.docker_calls() if not call.startswith('ps ')]
    assert acting == ['stop mcp1 proxy1'], acting


def test_every_mcp_compose_call_is_pinned_to_the_root_checkout_and_reads_no_env_file():
    """P7 and A5: -p, --project-directory and -f under AGENT_MCP_REPO_HOST_PATH, --env-file /dev/null."""
    folded = MAKEFILE.read_text(encoding='utf-8').replace('\\\n', ' ')
    value = re.search(r'^AGENT_MCP_COMPOSE = (.*)$', folded, re.MULTILINE).group(1)
    assert re.search(
        r'docker compose -p \$\(AGENT_MCP_PROJECT\) --project-directory "\$\(AGENT_MCP_REPO_HOST_PATH\)" '
        r'--env-file /dev/null\s+-f "\$\(AGENT_MCP_REPO_HOST_PATH\)/docker-compose\.agent-mcp\.yaml"\s*$',
        value,
    ), value
    assert _make_variable('AGENT_MCP_PROJECT') == MCP_PROJECT
    assert _make_variable('AGENT_MCP_REPO_HOST_PATH') == (
        '$(patsubst %/.git,%,$(shell git rev-parse --path-format=absolute --git-common-dir))'
    )
    uses = [
        line
        for line in folded.splitlines()
        if 'docker-compose.agent-mcp.yaml' in line and not line.lstrip().startswith('#')
    ]
    assert uses == [f'AGENT_MCP_COMPOSE = {value}'], f'the MCP compose file is loaded outside AGENT_MCP_COMPOSE: {uses}'
    recipe_uses = [line for line in folded.splitlines() if line.startswith('\t') and '$(AGENT_MCP_COMPOSE)' in line]
    assert len(recipe_uses) == 2, recipe_uses


def test_the_mcp_image_builds_from_the_working_tree_context():
    """A5, addendum 9 (1): context '.', the MCP's own Dockerfile; no git archive, ref or remote context."""
    build = load(AGENT_MCP_FILE)['services']['agent_mcp']['build']
    assert build['context'] == '.' and build['dockerfile'] == 'tools/agent_mcp/Dockerfile'
    for target in (
        'agent-mcp-up',
        'agent-mcp-rebuild',
        'agent-build',
        'agent-mcp-paths',
        'agent-mcp-network',
        'agent-mcp-share',
    ):
        for line in _make_recipe(target):
            assert not re.search(r'\bgit\s+(archive|clone|fetch|checkout|worktree|stash|show)\b', line), (
                f'{target}: {line}'
            )
            assert '://' not in line and 'git@' not in line, f'{target} builds from a remote context: {line}'


def test_agent_build_rebuilds_the_mcp_as_a_warning_recipe_line_after_the_devcontainer(host: Host):
    """P6: a RECIPE line after the devcontainer build, guarded by AGENT_MCP != off, warning on failure."""
    assert 'agent-mcp-rebuild' not in _make_prerequisites('agent-build')
    result = _make(host, 'agent-build', STUB_MCP_COMPOSE_EXIT='1')
    assert result.returncode == 0 and 'WARNING' in result.stderr and 'make agent-mcp-rebuild' in result.stderr, (
        result.stderr
    )
    calls = host.docker_calls()
    build = calls.index(f'{_agent_compose_call()} build')
    rebuild = next(index for index, call in enumerate(calls) if call.startswith(f'compose -p {MCP_PROJECT}'))
    assert build < rebuild, 'the MCP rebuild ran before the devcontainer build'


def test_agent_build_skips_the_mcp_with_agent_mcp_off_and_stops_on_a_failed_devcontainer_build(host: Host):
    result = _make(host, 'agent-build', AGENT_MCP='off')
    assert result.returncode == 0 and not _mcp_compose_calls(host)
    host.log.unlink()
    result = _make(host, 'agent-build', STUB_AGENT_BUILD_EXIT='1')
    assert result.returncode == 2 and not _mcp_compose_calls(host), (
        'the MCP was rebuilt after a failed devcontainer build'
    )


# --- R3 (tj-ix1hbl): AGENT_COMPOSE's flags, pinned from literals and from both branches -------------
#
# WHY THESE ARE HERE AND NOT FOLDED INTO THE THREE ASSERTIONS ABOVE. _agent_compose_call() reads
# AGENT_COMPOSE back out of make, so the `up -d`, `down` and `build` pins keep WHICH calls happen and
# in WHAT ORDER -- and would agree with ANY value, including one with no --env-file at all. Before
# this, the compose-file identity survived only INCIDENTALLY, through the docker stub's literal glob
# at line 67 of this file. The surviving pin was not the intended one.

DEVCONTAINER_COMPOSE_PATH = '.devcontainer/compose.yml'
ROOT_ENV_FILE_NAME = '.env'
NO_ENV_FILE = '/dev/null'


def test_agent_compose_carries_an_env_file_flag_and_the_devcontainers_own_compose_file():
    """R3, from the Makefile's LITERALS: the flag set, and that nothing else loads that compose file.

    WHAT THE FLAG SET IS FOR, so whoever reads a red here knows whether to widen it or to revert:
      -f .devcontainer/compose.yml
          the devcontainer's own file -- the one whose environment: block names the allowed keys one
          by one (pinned in test_agent_mcp_compose.py). Dropping or redirecting it moves the whole
          key-set guard off the file that is actually started.
      --env-file $(AGENT_ENV_FILE)
          compose takes its project directory from the FIRST compose file's directory, so that -f
          makes `.devcontainer` the project directory even though make runs from the repo root. The
          env file compose would read BY DEFAULT is therefore `.devcontainer/.env` and NOT the repo
          root's `.env`, where the two dev credentials live (Makefile:459-465). The flag names the
          root file explicitly; without it the container starts with both sentinels, which is a
          working-looking devcontainer with a dead password.

    A NEW FLAG REDS THIS ON PURPOSE. If the user's ruling on tj-oxxl8a adds `-p` for the agent
    devcontainer's compose project name, widening this is part of that work, in the same feature. Any
    other addition is a revert and not a widening -- and `--env-file` pointing anywhere but the root
    file is the documented trap, not a new flag.
    """
    value = _make_variable('AGENT_COMPOSE')
    assert re.fullmatch(
        rf'docker compose --env-file \$\(AGENT_ENV_FILE\) -f {re.escape(DEVCONTAINER_COMPOSE_PATH)}', value
    ), (
        f'AGENT_COMPOSE is `{value}`: it must be `docker compose --env-file $(AGENT_ENV_FILE) '
        f"-f {DEVCONTAINER_COMPOSE_PATH}` -- see this test's docstring for what each flag is for"
    )
    folded = MAKEFILE.read_text(encoding='utf-8').replace('\\\n', ' ')
    uses = [
        line for line in folded.splitlines() if DEVCONTAINER_COMPOSE_PATH in line and not line.lstrip().startswith('#')
    ]
    assert uses == [f'AGENT_COMPOSE := {value}'], (
        f'the devcontainer compose file is loaded outside AGENT_COMPOSE: {uses}. A second invocation would '
        'not carry --env-file, and would start the container with both credential sentinels'
    )


@pytest.mark.parametrize('root_env', [True, False], ids=['root-env-present', 'root-env-absent'])
def test_agent_env_file_is_the_absolute_root_env_file_or_dev_null(tmp_path: Path, root_env: bool):
    """R3's conditional, BOTH branches, run for real.

    AGENT_ENV_FILE is `$(if $(wildcard $(CURDIR)/.env),...)`, so only ONE branch is exercised on any
    given checkout -- a literal pin would pass in a worktree with no .env and fail on the user's main
    checkout, which is exactly why the three assertions above this section were loosened. Make is run
    from a tmp directory instead, so the branch is chosen by this fixture rather than by whether the
    machine happens to have a root .env.

    ABSOLUTE on purpose: compose has resolved a relative --env-file against the invoking directory in
    some versions and against the project directory in others (Makefile:467-469). /dev/null on a
    checkout with no root .env, because compose refuses outright when a named --env-file is missing,
    and a devcontainer you cannot stop is a worse failure than a credential you do not have.
    """
    if root_env:
        (tmp_path / ROOT_ENV_FILE_NAME).write_text('# never read into make; compose interpolates on the host\n')
    expected = str(tmp_path / ROOT_ENV_FILE_NAME) if root_env else NO_ENV_FILE
    value = _expanded_make_variable('AGENT_ENV_FILE', tmp_path, _subprocess_env())
    assert value == expected, f'AGENT_ENV_FILE is {value!r} with root .env present={root_env}, not {expected!r}'
    assert Path(value).is_absolute(), f'a relative --env-file resolves differently across compose versions: {value}'
    assert '.devcontainer' not in value, (
        f'--env-file points into .devcontainer ({value}): that is the file compose reads BY DEFAULT and the '
        'one place the two dev credentials are NOT, so this would start the container with both sentinels'
    )
    expanded = _expanded_make_variable('AGENT_COMPOSE', tmp_path, _subprocess_env())
    assert expanded == f'docker compose --env-file {expected} -f {DEVCONTAINER_COMPOSE_PATH}', expanded


# --- M4: the host path check, run for real -----------------------------------------------------------


def _layout(host: Host, case: str) -> dict[str, Path]:
    root = host.root
    paths = {'repo': root / 'repo', 'home': root / 'home', 'share': root / 'share', 'stack': root / 'stack'}
    nest = {
        'stack inside the repository': ('stack', 'repo'),
        'repository inside the stack dir': ('repo', 'stack'),
        'stack inside AGENT_HOME_PATH': ('stack', 'home'),
        'AGENT_HOME_PATH inside the stack dir': ('home', 'stack'),
        'stack inside the share': ('stack', 'share'),
        'share inside the stack dir': ('share', 'stack'),
        'share inside AGENT_HOME_PATH': ('share', 'home'),
        'AGENT_HOME_PATH inside the share': ('home', 'share'),
        'share inside the repository': ('share', 'repo'),
        'repository inside the share': ('repo', 'share'),
        'AGENT_HOME_PATH inside the repository': ('home', 'repo'),
    }.get(case)
    if nest:
        inner, outer = nest
        paths[inner] = paths[outer] / f'nested_{inner}'
    for name in ('repo', 'share', 'home', 'stack'):
        paths[name].mkdir(parents=True, exist_ok=True, mode=0o700)
        paths[name].chmod(0o700)
    return paths


_BAD_LAYOUTS = [
    'stack inside the repository',
    'repository inside the stack dir',
    'stack inside AGENT_HOME_PATH',
    'AGENT_HOME_PATH inside the stack dir',
    'stack inside the share',
    'share inside the stack dir',
    'share inside AGENT_HOME_PATH',
    'AGENT_HOME_PATH inside the share',
    'share inside the repository',
    'repository inside the share',
    'AGENT_HOME_PATH inside the repository',
]


def _paths_check(host: Host, paths: dict[str, Path]) -> subprocess.CompletedProcess:
    return _make(
        host,
        'agent-mcp-paths',
        f'AGENT_MCP_REPO_HOST_PATH={paths["repo"]}',
        f'AGENT_HOME_PATH={paths["home"]}',
        f'AGENT_MCP_SHARE_PATH={paths["share"]}',
        f'AGENT_MCP_STACK_DIR={paths["stack"]}',
        paths=False,
    )


def test_the_path_check_accepts_a_sane_layout(host: Host):
    result = _paths_check(host, _layout(host, 'sane'))
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('case', _BAD_LAYOUTS)
def test_the_path_check_refuses_every_overlap(host: Host, case: str):
    """M4, addendum 11 R2: docker-free, so run for real; refused with make's 2, naming both paths."""
    paths = _layout(host, case)
    result = _paths_check(host, paths)
    assert result.returncode == 2, f'{case}: exit {result.returncode}, {result.stderr}'
    assert 'must lie outside' in result.stderr


@pytest.mark.parametrize('target', ['agent-mcp-up', 'agent-mcp-rebuild'])
def test_every_start_and_rebuild_runs_the_path_check_first(target: str):
    assert 'agent-mcp-paths' in _make_prerequisites(target)
    assert 'agent-mcp-share' in _prerequisite_closure(target)
