"""Every docker command the verbs run: the fixed prefix, the snapshot, the trusted files -- and nothing else.

tj-c4mosr.5 body bullets 1 and 7, items D1 (01:40 (1)), S1, G1, (3), S2, O3, D3 (6), (13), and the
reviewed-suppression pin (02:32). Design: ADR tj-4rr0la section 3 and 5(a), addenda 1-6.

Every verb is RUN here, through AgentStack with a fake docker runner -- so what is pinned is the argv
the server really hands the daemon, not what a builder function returns when called in isolation.
"""

import ast
import os
import posixpath
import re
import shlex
from itertools import pairwise
from pathlib import Path

import pytest
import yaml

from common.tests import compose_model
from common.tests.test_ci_invariants import (
    _dockerfile_copy_sources,
    _dockerignore_pattern_regex,
    _dockerignore_rules,
    _is_excluded_from_context,
    _make_variable,
    _short_volume_source,
)
from tools.agent_mcp import runner, stack
from tools.agent_mcp.tests.harness import (
    DOCKERLESS_VERBS,
    REPO_ROOT,
    VERB_SAMPLES,
    WORKTREE_NAME,
    clearing_hook,
    default_response,
    make_rig,
    populate_data,
    record_state,
    step_prefix_length,
)


pytestmark = pytest.mark.build_infra

MCP_DIR = REPO_ROOT / 'tools' / 'agent_mcp'
MCP_DOCKERFILE = MCP_DIR / 'Dockerfile'
MCP_DOCKERIGNORE = MCP_DIR / 'Dockerfile.dockerignore'
OVERLAY = REPO_ROOT / 'docker-compose.agent-stack.yaml'
FAKE_OVERLAY = REPO_ROOT / 'docker-compose.fake.yaml'
# Spelled out, not read from stack.py: a test that took the expected prefix from the module under
# test would agree with any change to it.
EXPECTED_PROJECT = 'trader_joe_agent_stack'
EXPECTED_TRUSTED_DIR = '/opt/agent_mcp/compose'
EXPECTED_DOCKER = '/usr/local/bin/docker'
# The agent stack's file list, in order (ADR tj-4rr0la addendum 3 (3); tj-vhboky.61): base, test
# client, the agent-stack overlay, then the fake-mode overlay LAST, so data_ingest always runs FakeRead.
EXPECTED_COMPOSE_FILES = (
    'docker-compose.yaml',
    'docker-compose.test-client.yaml',
    'docker-compose.agent-stack.yaml',
    'docker-compose.fake.yaml',
)


def _base_inspect_argv(ref: str) -> list[str]:
    """The one plain-docker inspect argv the ensure-bases step may run (ADR tj-4rr0la addendum 14 (3))."""
    return [EXPECTED_DOCKER, 'image', 'inspect', '--format', '{{.Id}}', ref]


def _base_pull_argv(ref: str) -> list[str]:
    """The one plain-docker pull argv, run only after its inspect failed."""
    return [EXPECTED_DOCKER, 'pull', ref]


def _bases_absent(step: stack.Step) -> runner.ProcessResult:
    """Every base inspect fails (the daemon lacks it), so ensure-bases pulls each ref; the rest as default."""
    if list(step.argv[1:3]) == ['image', 'inspect']:
        return runner.ProcessResult(1, b'', b'Error: No such image')
    return default_response(step)


def _run_verb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, respond=default_response):
    rig = make_rig(tmp_path, monkeypatch)
    rig.docker.respond = respond
    record_state(rig.layout)
    populate_data(rig.layout)
    rig.docker.hook = clearing_hook(rig.layout)
    result = rig.call(verb, dict(VERB_SAMPLES[verb]))
    return rig, result


def test_every_verb_has_a_sample():
    """The parameterised tests below run every verb: a new verb without a sample goes red here."""
    agent = runner.AgentStack(settings=None)  # type: ignore[arg-type] -- only the verb table is read
    assert set(agent.verbs) == set(VERB_SAMPLES), f'verbs {agent.verbs} vs samples {sorted(VERB_SAMPLES)}'
    assert set(runner.VERB_SCHEMAS) == set(VERB_SAMPLES) == set(runner.VERB_TIMEOUT_SECONDS)


@pytest.mark.parametrize('verb', sorted(VERB_SAMPLES))
def test_every_verb_runs_only_the_fixed_compose_prefix_over_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    """Body bullet 1, S1 and G1: -p trader_joe_agent_stack, the snapshot, the generated env, the trusted -f list.

    Every compose step of every verb starts `/usr/local/bin/docker compose -p trader_joe_agent_stack
    --project-directory <stack>/source --env-file <stack>/agent_stack.env` and then -f for each
    trusted compose file, in the Makefile's order. The ONLY other argv a verb may run is the
    ensure-bases pair of ADR tj-4rr0la addendum 14 (3), exactly `docker image inspect --format {{.Id}}
    <ref>` or `docker pull <ref>` of a BASE_IMAGES ref (re-pinned, tj-c4mosr.14); any other plain
    docker argv is red. Run with every base absent, so both forms appear. Every step, either kind,
    runs with cwd <stack>/source, and no argv word names a path under the repository or any worktree.
    """
    rig, result = _run_verb(tmp_path, monkeypatch, verb, _bases_absent)
    layout = rig.layout
    assert result['status'] == 'ok', result
    if verb in DOCKERLESS_VERBS:
        assert rig.docker.steps == [], f'{verb} ran docker: {rig.docker.steps}'
        return
    assert rig.docker.steps, f'{verb} ran no docker step, so there is nothing to pin'
    expected = [
        '/usr/local/bin/docker',
        'compose',
        '-p',
        EXPECTED_PROJECT,
        '--project-directory',
        str(layout.stack_dir / 'source'),
        '--env-file',
        str(layout.stack_dir / 'agent_stack.env'),
    ]
    for name in EXPECTED_COMPOSE_FILES:
        expected += ['-f', f'{EXPECTED_TRUSTED_DIR}/{name}']
    base_argvs = [_base_inspect_argv(ref) for ref in stack.BASE_IMAGES] + [
        _base_pull_argv(ref) for ref in stack.BASE_IMAGES
    ]
    for step in rig.docker.steps:
        argv = list(step.argv)
        assert step.cwd == layout.stack_dir / 'source', f'{verb} runs a step with cwd {step.cwd}, not the snapshot'
        repository_paths = [word for word in argv if str(layout.repo) in word or str(layout.worktree) in word]
        assert not repository_paths, f'{verb} hands the daemon a repository or worktree path: {repository_paths}'
        if argv in base_argvs:
            continue
        assert argv[: len(expected)] == expected, f'{verb}: {shlex.join(argv)}'
        assert argv.count('-p') == 1 and argv.count('--project-directory') == 1 and argv.count('--env-file') == 1


# The ensure-bases inspects, once per verb before its first step that can build (ADR tj-4rr0la
# addenda 14 (3) and 15). Bases present here (FakeDocker's default exit 0), so nothing is pulled.
_INSPECTS = [_base_inspect_argv(ref) for ref in stack.BASE_IMAGES]

_EXPECTED_TAILS = {
    'stack_up': [
        *_INSPECTS,
        ['build', 'data_store', 'data_ingest', 'test_client'],
        # tj-zgq5v2: the infrastructure plain, then the snapshot-bound services force-recreated. Exact,
        # not a prefix (below), so a flag or a service moved between the two is red.
        ['up', '-d', '--wait', '--wait-timeout', '300', 'postgres', 'kafka'],
        ['up', '-d', '--wait', '--wait-timeout', '300', '--force-recreate', '--no-deps', 'data_store', 'data_ingest'],
    ],
    'stack_down': [['down', '--remove-orphans']],
    'stack_wipe': [['down', '--remove-orphans'], ['run'], ['run']],
    'migrate': [
        ['ps', '-q', 'postgres'],
        *_INSPECTS,
        ['run', '--rm', '--no-deps', 'data_store', '/code/.venv/bin/alembic', 'upgrade', 'head'],
    ],
    'migrate_status': [
        ['ps', '-q', 'postgres'],
        *_INSPECTS,
        ['run', '--rm', '--no-deps', 'data_store', '/code/.venv/bin/alembic', 'current'],
        ['run', '--rm', '--no-deps', 'data_store', '/code/.venv/bin/alembic', 'history'],
    ],
    'run_system_tests': [
        *_INSPECTS,
        ['run', '--rm', '--no-deps', '--build', 'test_client', 'tests/system/test_one.py'],
    ],
    # tj-irhy0a.22 V1: the bases, the build, then the ONE producer invocation make seed-dump also
    # spells (ADR tj-4rr0la addendum 10 (1)), spelled out here; --date only when given (below).
    'seed_dump': [
        *_INSPECTS,
        ['build', 'test_client'],
        ['run', '--rm', '-T', '--entrypoint', '/code/.venv/bin/python', 'test_client', '-m', 'data.store.seeds'],
    ],
    'logs': [['logs', '--no-color', '--tail', '50', 'postgres']],
    'ps': [['ps', '--all']],
}


def _tail(step: stack.Step) -> list[str]:
    """A compose step's words after the fixed prefix; a plain docker step (the bases) whole."""
    if list(step.argv[:2]) == [EXPECTED_DOCKER, 'compose']:
        return list(step.argv[step_prefix_length() :])
    return list(step.argv)


@pytest.mark.parametrize('verb', sorted(_EXPECTED_TAILS))
def test_every_verb_runs_exactly_its_fixed_tails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str):
    """After the prefix, each step is a fixed tail from constants plus the verb's validated arguments.

    The ensure-bases inspects sit whole at their place in the list (re-pinned, tj-c4mosr.14).
    """
    rig, _ = _run_verb(tmp_path, monkeypatch, verb)
    tails = [_tail(step) for step in rig.docker.steps]
    expected = _EXPECTED_TAILS[verb]
    assert len(tails) == len(expected), f'{verb} ran {tails}'
    for tail, want in zip(tails, expected, strict=True):
        assert tail[: len(want)] == want, f'{verb} ran {tail}, expected it to start {want}'
    if verb == 'seed_dump':
        # Exactly, not a prefix: with no date given, nothing follows the module name.
        assert tails[-2:] == expected[-2:], tails
    if verb == 'stack_up':
        # Exactly, not a prefix: the build names every built service and each up its whole service list.
        assert tails[-3:] == expected[-3:], tails


def test_every_verb_with_fixed_tails_is_covered():
    """A verb that runs docker and has no _EXPECTED_TAILS entry would escape the tail pin."""
    assert set(_EXPECTED_TAILS) == set(VERB_SAMPLES) - DOCKERLESS_VERBS


def test_stack_wipe_clears_only_the_data_mounts_as_root_in_one_off_containers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig, result = _run_verb(tmp_path, monkeypatch, 'stack_wipe')
    assert result['status'] == 'ok', result
    runs = [list(step.argv[step_prefix_length() :]) for step in rig.docker.steps[1:]]
    assert runs == [
        [
            'run',
            '--rm',
            '--no-deps',
            '--user',
            '0',
            '--entrypoint',
            'sh',
            service,
            '-c',
            stack._CLEAR_SCRIPT,
            'clear',
            target,
        ]
        for service, target in stack.DATA_MOUNTS
    ]


# --- the constants agree with the files that define them ----------------------------------------


def _agent_stack_compose() -> list[str]:
    return shlex.split(_make_variable('AGENT_STACK_COMPOSE'))


def test_the_module_constants_and_the_makefile_agree():
    """Body bullet 7: stack.PROJECT / COMPOSE_FILES mirror AGENT_STACK_PROJECT / AGENT_STACK_COMPOSE."""
    words = _agent_stack_compose()
    assert words[:2] == ['docker', 'compose'], words
    assert words[words.index('-p') + 1] == '$(AGENT_STACK_PROJECT)', words
    assert _make_variable('AGENT_STACK_PROJECT') == stack.PROJECT == EXPECTED_PROJECT
    files = [value for flag, value in pairwise(words) if flag in ('-f', '--file')]
    assert tuple(files) == stack.COMPOSE_FILES, f'AGENT_STACK_COMPOSE loads {files}, the server {stack.COMPOSE_FILES}'
    assert tuple(files) == EXPECTED_COMPOSE_FILES, f'AGENT_STACK_COMPOSE loads {files}'
    assert files[-2:] == [OVERLAY.name, FAKE_OVERLAY.name], (
        'the agent-stack overlay, then the fake-mode overlay LAST (ADR tj-4rr0la addendum 3 (3), tj-vhboky.61)'
    )


def test_the_prefix_names_only_the_trusted_copies():
    """D1: every -f lies under TRUSTED_COMPOSE_DIR, never the worktree, in COMPOSE_FILES order."""
    assert str(stack.TRUSTED_COMPOSE_DIR) == EXPECTED_TRUSTED_DIR
    assert str(stack.TRUSTED_DOCKERFILE) == f'{EXPECTED_TRUSTED_DIR}/Dockerfile'
    prefix = stack.compose_prefix(Path('/stack'), Path('/stack/agent_stack.env'))
    files = [value for flag, value in pairwise(prefix) if flag == '-f']
    assert files == [f'{EXPECTED_TRUSTED_DIR}/{name}' for name in stack.COMPOSE_FILES]
    assert stack.DOCKER == '/usr/local/bin/docker' and stack.DOCKER_HOST == 'tcp://socket_proxy:2375'


def _mcp_dockerfile_copies() -> list[tuple[list[str], str]]:
    """(sources, destination) of every context COPY in tools/agent_mcp/Dockerfile."""
    copies = []
    for line in MCP_DOCKERFILE.read_text(encoding='utf-8').replace('\\\n', ' ').splitlines():
        words = shlex.split(line, comments=True)
        if words[:1] != ['COPY'] or any(word.startswith('--from') for word in words[1:]):
            continue
        operands = [word for word in words[1:] if not word.startswith('--')]
        copies.append((operands[:-1], operands[-1]))
    return copies


def test_the_mcp_image_bakes_in_exactly_the_trusted_compose_files_and_dockerfile():
    """D1 and O3: the image COPYs COMPOSE_FILES and the root Dockerfile into TRUSTED_COMPOSE_DIR."""
    into_trusted = {}
    for sources, destination in _mcp_dockerfile_copies():
        if destination.rstrip('/') == EXPECTED_TRUSTED_DIR:
            for source in sources:
                into_trusted[source] = f'{EXPECTED_TRUSTED_DIR}/{source}'
        elif destination.startswith(EXPECTED_TRUSTED_DIR + '/'):
            for source in sources:
                into_trusted[source] = destination
    assert set(into_trusted) == {*stack.COMPOSE_FILES, 'Dockerfile'}, into_trusted
    assert into_trusted['Dockerfile'] == str(stack.TRUSTED_DOCKERFILE)


def test_the_mcp_image_bakes_in_the_fake_overlay_and_admits_it_but_never_the_fakes():
    """tj-vhboky.61, D1: the MCP image carries its own copy of docker-compose.fake.yaml, never tests/.

    The verbs pass -f for every COMPOSE_FILES entry under TRUSTED_COMPOSE_DIR, so
    docker-compose.fake.yaml must be COPYd there and re-included by Dockerfile.dockerignore -- named
    literally, not through COMPOSE_FILES, so dropping it from the module and the image together still
    goes red. The fakes themselves reach the agent stack only through the snapshot's :ro mount: the
    MCP image never COPYs anything under tests/, and its build context leaves tests/fakes out.
    """
    copied = {source: destination for sources, destination in _mcp_dockerfile_copies() for source in sources}
    assert copied.get(FAKE_OVERLAY.name, '').rstrip('/') == EXPECTED_TRUSTED_DIR, copied
    rules = _dockerignore_rules(MCP_DOCKERIGNORE.read_text(encoding='utf-8'))
    assert not _is_excluded_from_context(FAKE_OVERLAY.name, rules), 'the MCP build context leaves out the fake overlay'
    assert [source for source in copied if source == 'tests' or source.startswith(('tests/', './tests'))] == []
    fakes = sorted(str(path.relative_to(REPO_ROOT)) for path in (REPO_ROOT / 'tests' / 'fakes').glob('*.py'))
    assert 'tests/fakes/ingest_launcher.py' in fakes, fakes
    assert [path for path in fakes if not _is_excluded_from_context(path, rules)] == []


def test_the_mcp_build_context_admits_the_trusted_files_and_nothing_live():
    """D1 and O3: Dockerfile.dockerignore re-includes each file the image COPYs, and leaves out env files and tests."""
    rules = _dockerignore_rules(MCP_DOCKERIGNORE.read_text(encoding='utf-8'))
    sent = [
        *stack.COMPOSE_FILES,
        'Dockerfile',
        'pyproject.toml',
        'uv.lock',
        'tools/__init__.py',
        *(f'tools/agent_mcp/{path.name}' for path in MCP_DIR.glob('*.py')),
    ]
    assert [path for path in sent if _is_excluded_from_context(path, rules)] == []
    kept_out = [
        '.env',
        'data/store/.env',
        'data/ingest/.env',
        'volumes/trader_joe/postgres/PG_VERSION',
        'tools/agent_mcp/tests/test_commands.py',
        '.claude/worktrees/x/docker-compose.yaml',
        'common/__init__.py',
        '.git/config',
    ]
    assert [path for path in kept_out if not _is_excluded_from_context(path, rules)] == []


# --- the build-context WALK: what BuildKit opens, not only what it sends (tj-c4mosr.11) -----------
#
# Re-implements two upstream rules, so what is pinned is the walk BuildKit's sender performs over
# the real checkout -- the defect at 32abf36 left the include set exactly right and still opened
# every directory, including ${DATA_DIR}'s unreadable postgres directory.
#
# moby/patternmatcher ignorefile.ReadAll: '#' lines and blanks dropped, '!' marks an exception,
# the pattern is filepath.Clean-ed and a leading '/' stripped.
#
# tonistiigi/fsutil filter.go: an excluded DIRECTORY is skipped -- never opened -- only while
# onlyPrefixExcludeExceptions holds AND no exception pattern lies under it (pattern + '/' starts with
# dir + '/'). onlyPrefixExcludeExceptions is false as soon as one exception, after a trailing '/**'
# and then a trailing '/*' is stripped, still contains a character of "*[]?^\" (patternChars, with
# the backslash added on a '/'-separated platform).

_FSUTIL_PATTERN_CHARS = '*[]?^\\'


def _dockerignore_patterns(text: str) -> list[tuple[bool, str]]:
    """(is_exception, cleaned pattern) per line, as ignorefile.ReadAll yields them."""
    patterns = []
    for line in text.splitlines():
        if line.startswith('#'):
            continue
        stripped = line.strip()
        if not stripped:
            continue
        exception = stripped.startswith('!')
        if exception:
            stripped = stripped[1:].strip()
        cleaned = posixpath.normpath(stripped)
        if len(cleaned) > 1 and cleaned.startswith('/'):
            cleaned = cleaned[1:]
        patterns.append((exception, cleaned))
    return patterns


def _pruning_offenders(patterns: list[tuple[bool, str]]) -> list[str]:
    """The exception patterns that turn fsutil's onlyPrefixExcludeExceptions off -- empty when it holds."""
    offenders = []
    for exception, pattern in patterns:
        if not exception:
            continue
        stripped = pattern.removesuffix('/**').removesuffix('/*')
        if any(char in _FSUTIL_PATTERN_CHARS for char in stripped):
            offenders.append(f'!{pattern}')
    return offenders


def _walk_build_context(root: Path, text: str, may_open: set[str]) -> tuple[list[str], set[str]]:
    """The filtered walk fsutil performs over `root`: (directories opened, files sent).

    Stops at the first directory opened outside `may_open`: that is already the failure, and an
    unpruned walk of a real checkout would otherwise descend .venv, .git, every sibling worktree
    and the data directory -- where it meets EACCES exactly as the host build did.
    """
    patterns = _dockerignore_patterns(text)
    rules = [(exception, _dockerignore_pattern_regex(pattern)) for exception, pattern in patterns if pattern != '.']
    exceptions = [pattern for exception, pattern in patterns if exception]
    prune = not _pruning_offenders(patterns)
    opened, sent, pending = [], set(), ['.']
    while pending:
        directory = pending.pop()
        opened.append(directory)
        if directory not in may_open:
            break
        for entry in sorted(os.scandir(root / directory), key=lambda entry: entry.name):
            path = entry.name if directory == '.' else f'{directory}/{entry.name}'
            excluded = _is_excluded_from_context(path, rules)
            if entry.is_dir(follow_symlinks=False):
                under_an_exception = any(f'{pattern}/'.startswith(f'{path}/') for pattern in exceptions)
                if not (excluded and prune and not under_an_exception):
                    pending.append(path)
            elif not excluded:
                sent.add(path)
    return opened, sent


def _mcp_copy_sources_in_checkout() -> set[str]:
    """Every context COPY source of tools/agent_mcp/Dockerfile, globbed against the real checkout."""
    files = set()
    for sources, _ in _mcp_dockerfile_copies():
        for source in sources:
            matched = [path for path in REPO_ROOT.glob(source) if path.is_file()]
            assert matched, f'COPY source {source} matches nothing in the checkout'
            files |= {path.relative_to(REPO_ROOT).as_posix() for path in matched}
    return files


def test_no_exception_line_in_the_mcp_dockerignore_disables_pruning():
    """tj-c4mosr.11: one wildcard in one '!' line turns pruning off for the whole file.

    Then BuildKit's sender opens every directory of the checkout, and the host build died on
    'open .../volumes/trader_joe/postgres: permission denied'. A wildcard on an EXCLUDE line is
    fine; only exception lines govern pruning.
    """
    patterns = _dockerignore_patterns(MCP_DOCKERIGNORE.read_text(encoding='utf-8'))
    assert any(exception for exception, _ in patterns), 'no exception lines: this is no longer an allow-list'
    assert _pruning_offenders(patterns) == [], (
        'these exception lines keep a wildcard after the trailing-glob strip, so fsutil prunes no '
        'excluded directory and the build walks the data directory; re-include a directory instead'
    )


def test_the_mcp_build_context_walk_opens_only_the_copy_directories_and_sends_exactly_the_copy_sources():
    """tj-c4mosr.11 over the real checkout: the walk opens '.' and the COPY sources' directories, nothing else.

    Both sides are derived from tools/agent_mcp/Dockerfile, so a COPY source added to the Dockerfile
    and the dockerignore together stays green, and one added to only one of them goes red.
    """
    expected = _mcp_copy_sources_in_checkout()
    may_open = {'.'} | {posixpath.dirname(path) or '.' for path in expected}
    opened, sent = _walk_build_context(REPO_ROOT, MCP_DOCKERIGNORE.read_text(encoding='utf-8'), may_open)
    assert [directory for directory in opened if directory not in may_open] == [], (
        f'the build-context walk opens directories outside {sorted(may_open)}'
    )
    assert sent == expected, (
        f'sent but not COPYed: {sorted(sent - expected)}; COPYed but not sent: {sorted(expected - sent)}'
    )


def _overlay_data_targets() -> dict[str, str]:
    targets = {}
    for service, spec in (yaml.safe_load(OVERLAY.read_text(encoding='utf-8'))['services'] or {}).items():
        for entry in (spec or {}).get('volumes') or []:
            source = _short_volume_source(entry)
            if source.startswith('${DATA_DIR'):
                target = entry[len(source) + 1 :].split(':')[0]
                targets[service] = os.path.normpath(target)
    return targets


def test_data_mounts_are_the_overlays_data_dir_targets():
    """(3): the clear steps name exactly the services and container targets the overlay binds from DATA_DIR."""
    assert {service: os.path.normpath(target) for service, target in stack.DATA_MOUNTS} == _overlay_data_targets()
    assert set(dict(stack.DATA_MOUNTS)) == {'postgres', 'kafka'}


def _relative_bind_sources(path: Path) -> set[str]:
    sources = set()
    for spec in (yaml.safe_load(path.read_text(encoding='utf-8')).get('services') or {}).values():
        for entry in (spec or {}).get('volumes') or []:
            source = _short_volume_source(entry) if isinstance(entry, str) else str(entry.get('source', ''))
            if source.startswith('.'):
                sources.add(os.path.normpath(source))
    return sources - {'.'}


def test_snapshot_sources_are_exactly_what_the_trusted_files_read_from_the_project_directory():
    """S2: SNAPSHOT_SOURCES == the Dockerfile's COPY sources UNION the COMPOSE_FILES' relative bind sources.

    Parsed, so a new COPY or relative mount without an entry here goes red -- and an entry nothing
    reads goes red too. The build context '.' is the snapshot root itself.
    """
    expected = set(_dockerfile_copy_sources())
    for name in stack.COMPOSE_FILES:
        expected |= _relative_bind_sources(REPO_ROOT / name)
    assert {'data/store/migrations', 'tests/system', 'tests/fakes', 'data/ingest/app', 'pytest.ini'} <= expected, (
        expected
    )
    assert set(stack.SNAPSHOT_SOURCES) == expected, (
        f'SNAPSHOT_SOURCES {sorted(stack.SNAPSHOT_SOURCES)} vs what the trusted files read {sorted(expected)}'
    )
    assert len(stack.SNAPSHOT_SOURCES) == len(set(stack.SNAPSHOT_SOURCES))


# --- stack_up recreates what binds the snapshot (tj-zgq5v2) -------------------------------------
#
# refresh_snapshot swaps the snapshot by rename and removes the old generation, so a running
# container's relative binds point at a removed directory; only a re-CREATION re-resolves them.
# Design: the architect's ruling on tj-zgq5v2; ADR tj-4rr0la addendum 5 ruling 1 (the validator
# mutates in place) and addenda 14-15 (the bases before the first building step).

# The run-per-call service: it binds the snapshot too, but run_system_tests and seed_dump create it
# fresh with `run --rm` after the refresh, so it is outside SERVICES and needs no recreate.
_RUN_PER_CALL_SERVICES = {'test_client'}


def _services_with_a_snapshot_bind() -> set[str]:
    """The services of the MERGED agent-stack model (stack.COMPOSE_FILES, in order) with a relative bind.

    Merged, not per file: volumes merge by container target, so a later file that replaces a relative
    bind with a DATA_DIR one removes it, and one that adds it (the fake overlay's tests/fakes) adds it.
    """
    model = compose_model.merge([compose_model.load(REPO_ROOT / name) for name in stack.COMPOSE_FILES])
    bound = set()
    for service, spec in model['services'].items():
        for entry in spec.get('volumes') or []:
            mount = compose_model.volume(entry)
            if mount['type'] == 'bind' and mount['source'].startswith('.'):
                bound.add(service)
    return bound


def test_snapshot_bound_services_are_the_long_running_services_with_a_relative_bind():
    """Ruling (1) and re-pin (b): SNAPSHOT_BOUND_SERVICES == the SERVICES binding the snapshot, parsed.

    So a new relative bind into a long-running service without an entry goes red, and an entry that
    binds nothing goes red too. Every other service with such a bind must be a run-per-call one.
    """
    bound = _services_with_a_snapshot_bind()
    assert {'data_store', 'data_ingest'} <= bound, bound
    assert len(stack.SNAPSHOT_BOUND_SERVICES) == len(set(stack.SNAPSHOT_BOUND_SERVICES))
    assert set(stack.SNAPSHOT_BOUND_SERVICES) == bound & set(stack.SERVICES), (
        f'SNAPSHOT_BOUND_SERVICES {sorted(stack.SNAPSHOT_BOUND_SERVICES)} vs the SERVICES with a relative '
        f'bind in the merged agent-stack model {sorted(bound & set(stack.SERVICES))}: stack_up would leave '
        f'the difference on a removed snapshot generation'
    )
    assert bound - set(stack.SERVICES) == _RUN_PER_CALL_SERVICES, (
        f'{sorted(bound - set(stack.SERVICES) - _RUN_PER_CALL_SERVICES)} bind the snapshot but are neither '
        f'started by stack_up nor created per call: decide which, and pin it'
    )


def _compose_tails(rig) -> list[tuple[stack.Step, list[str]]]:
    return [(step, _tail(step)) for step in rig.docker.steps if list(step.argv[:2]) == [EXPECTED_DOCKER, 'compose']]


def _up_services(tail: list[str]) -> list[str]:
    """The service operands of an `up` tail: the words after its options (`--wait-timeout` takes one)."""
    words, services = iter(tail[1:]), []
    for word in words:
        if word == '--wait-timeout':
            next(words)
        elif not word.startswith('-'):
            services.append(word)
    return services


def test_stack_up_force_recreates_exactly_the_snapshot_bound_services_after_the_infrastructure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Ruling (2) and re-pin (c), run through AgentStack.

    The last step is an `up --force-recreate --no-deps` of exactly SNAPSHOT_BOUND_SERVICES; the `up`
    before it names exactly the rest of SERVICES, with neither flag, so postgres and kafka keep their
    containers and each app container is created once; together they cover SERVICES. Both up steps
    wait, both are flagged as builds, and the base inspects run once, before the build.
    """
    rig, result = _run_verb(tmp_path, monkeypatch, 'stack_up')
    assert result['status'] == 'ok', result
    steps = _compose_tails(rig)
    assert [tail[0] for _, tail in steps] == ['build', 'up', 'up'], [tail for _, tail in steps]
    (_, build), (plain_step, plain), (recreate_step, recreate) = steps
    assert rig.docker.steps[-1] is recreate_step, 'the force-recreate must be the last step stack_up runs'
    assert '--force-recreate' in recreate and '--no-deps' in recreate, recreate
    assert '--force-recreate' not in plain and '--no-deps' not in plain, plain
    assert _up_services(recreate) == list(stack.SNAPSHOT_BOUND_SERVICES), recreate
    assert set(_up_services(plain)) == set(stack.SERVICES) - set(stack.SNAPSHOT_BOUND_SERVICES), plain
    assert sorted(_up_services(plain) + _up_services(recreate)) == sorted(stack.SERVICES)
    for tail in (plain, recreate):
        assert tail[1:5] == ['-d', '--wait', '--wait-timeout', '300'], tail
    assert plain_step.builds is True and recreate_step.builds is True
    inspects = [index for index, step in enumerate(rig.docker.steps) if list(step.argv) in _INSPECTS]
    assert len(inspects) == len(stack.BASE_IMAGES), 'the bases are ensured once per stack_up, not per up step'
    assert max(inspects) < rig.docker.steps.index(steps[0][0]), 'the inspects run before the build'
    assert build == ['build', *stack.BUILT_SERVICES]


@pytest.mark.parametrize('verb', sorted(set(VERB_SAMPLES) - DOCKERLESS_VERBS - {'stack_up'}))
def test_no_other_verb_recreates_or_restarts_a_running_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    """Ruling, rejected option: only stack_up recreates a running service.

    run_system_tests in particular must not restart the stack between a validator's focused runs --
    it recreates test_client alone, by `run`.
    """
    rig, result = _run_verb(tmp_path, monkeypatch, verb)
    assert result['status'] == 'ok', result
    for _, tail in _compose_tails(rig):
        assert tail[0] not in ('up', 'restart', 'create', 'start'), f'{verb} runs {tail}'
        assert '--force-recreate' not in tail and '--always-recreate-deps' not in tail, f'{verb} runs {tail}'


# --- git: plumbing only, hardened ---------------------------------------------------------------


def test_run_git_is_hardened_plumbing_with_a_fixed_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """D3: --no-pager, fsmonitor off, hooksPath /dev/null, no system config, no HOME, argument list, no shell."""
    seen = {}

    class _Done:
        returncode, stdout, stderr = 0, 'out', ''

    def fake_run(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return _Done()

    monkeypatch.setattr(stack.subprocess, 'run', fake_run)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('GIT_DIR', str(tmp_path / 'elsewhere'))
    assert stack.run_git(tmp_path, ['worktree', 'list', '--porcelain']) == 'out'
    assert seen['argv'] == [
        '/usr/bin/git',
        '--no-pager',
        '-c',
        'core.fsmonitor=false',
        '-c',
        'core.hooksPath=/dev/null',
        'worktree',
        'list',
        '--porcelain',
    ]
    assert seen['env'] == {
        'PATH': '/usr/bin:/bin',
        'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_TERMINAL_PROMPT': '0',
        'GIT_PAGER': 'cat',
        'LC_ALL': 'C',
    }
    assert 'HOME' not in seen['env'] and not seen.get('shell')
    assert seen['cwd'] == tmp_path and seen['timeout'] and seen['check'] is False


def _run_git_calls() -> list[ast.Call]:
    calls = []
    for path in sorted(MCP_DIR.glob('*.py')):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if isinstance(node, ast.Call) and getattr(node.func, 'attr', getattr(node.func, 'id', None)) == 'run_git':
                calls.append(node)
    return calls


def test_the_only_git_commands_are_worktree_list_and_cat_file_of_a_committed_default():
    """D3: every run_git call site passes one of the two plumbing commands, spelled as literals."""
    calls = _run_git_calls()
    assert len(calls) == 3, f'expected the three known run_git call sites, found {len(calls)}'
    for call in calls:
        args = call.args[1]
        assert isinstance(args, ast.List), ast.unparse(call)
        words = [ast.unparse(element) for element in args.elts]
        assert words in (["'worktree'", "'list'", "'--porcelain'"], ["'cat-file'", "'blob'", "f'HEAD:{source}'"]), (
            ast.unparse(call)
        )
    assert set(stack.ENV_DEFAULT_SOURCES.values()) == {
        '.env.default',
        'data/store/.env.default',
        'data/ingest/.env.default',
    }


# --- seed_dump and the error statuses ----------------------------------------------------------


def _server_error_statuses() -> set[str]:
    """server.ERROR_STATUSES, read by parsing: server.py imports the MCP SDK, which this venv omits."""
    tree = ast.parse((MCP_DIR / 'server.py').read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and [ast.unparse(target) for target in node.targets] == ['ERROR_STATUSES']:
            return set(ast.literal_eval(node.value.args[0]))
    raise AssertionError('server.py defines no ERROR_STATUSES')


def test_seed_dump_runs_and_answers_ok_and_the_error_statuses_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """(13), re-pinned by tj-irhy0a.22 item 4 ('Remove the not_available answer').

    seed_dump now runs docker and answers 'ok' on a valid bundle; the server's error statuses are
    still the five, so 'refused' (the producer's exit 3) and 'failed' (a refused bundle) read as
    errors and 'ok' does not.
    """
    rig = make_rig(tmp_path, monkeypatch)
    result = rig.call('seed_dump', {'worktree': WORKTREE_NAME})
    assert result['status'] == 'ok' and rig.docker.steps, result
    statuses = _server_error_statuses()
    assert statuses == {'refused', 'busy', 'failed', 'timeout', 'error'}
    assert 'ok' not in statuses and 'not_available' not in statuses


# --- the one reviewed suppression --------------------------------------------------------------

_NOSEMGREP_RULE = 'python.lang.security.audit.insecure-file-permissions.insecure-file-permissions'


def test_stack_py_carries_exactly_the_one_reviewed_suppression_pair():
    """02:32 note / ADR tj-4rr0la addendum 6: one nosemgrep and one nosec B103, both on the snapshot-dir fchmod.

    The rule for any suppression in this repository: one line, one named rule id, an architect
    ruling in the owning ADR. This pins that stack.py has no other.
    """
    source = (MCP_DIR / 'stack.py').read_text(encoding='utf-8')
    lines = source.splitlines()
    nosemgrep = [index for index, line in enumerate(lines) if 'nosemgrep' in line]
    b103 = [index for index, line in enumerate(lines) if re.search(r'nosec\s+B103\b', line)]
    assert len(nosemgrep) == 1 and len(b103) == 1, (nosemgrep, b103)
    assert lines[nosemgrep[0]].strip() == f'# nosemgrep: {_NOSEMGREP_RULE}'
    fchmod = b103[0]
    assert nosemgrep[0] == fchmod - 1, 'the nosemgrep must sit on the line directly above the fchmod it covers'
    assert lines[fchmod].strip().startswith('os.fchmod(fd, 0o755)'), lines[fchmod]
    function = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == '_make_snapshot_dir'
    )
    assert function.lineno <= fchmod + 1 <= function.end_lineno, 'the suppressed line is outside _make_snapshot_dir'
    nosec = [line for line in lines if re.search(r'#\s*nosec\b', line)]
    assert all(re.search(r'#\s*nosec\s+B\d{3}\b', line) for line in nosec), f'a blanket nosec: {nosec}'
