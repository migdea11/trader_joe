"""The base images: digest-pinned, listed once in stack.BASE_IMAGES, ensured before every build.

The trusted Dockerfile pins each external ref by digest, and the MCP makes each present on the daemon
before every step that can build.

tj-c4mosr.14 pins (1)-(8) as revised by the architect's FINAL DESIGN note. Design: ADR tj-4rr0la
addenda 13-15. Pin (4), agent_mcp's two internal networks, is
common/tests/test_agent_mcp_compose.py::test_agent_mcp_joins_only_its_two_internal_networks and is
not duplicated here; that every step, the base ones included, runs in DOCKER_ENV is
test_runner.py::test_run_process_hands_the_child_exactly_docker_env together with the sweep below
showing the base steps pass through the same `run` as every other step.

Expected values are literals wherever the design names them: a test that read them from stack.py
would agree with any change to it. The digests are the builder's to source, never this file's, so
only their SHAPE and their agreement with the Dockerfile are pinned.
"""

import ast
import asyncio
import inspect
import re
import shlex
from pathlib import Path

import pytest
import yaml

from common.tests.test_ci_invariants import UV_PIN_SOURCES, _dockerfile_stages, _uv_pins
from tools.agent_mcp import runner, stack
from tools.agent_mcp.tests.harness import (
    DOCKERLESS_VERBS,
    REPO_ROOT,
    VERB_SAMPLES,
    FakeDocker,
    clearing_hook,
    default_response,
    make_rig,
    populate_data,
    record_state,
    step_prefix_length,
)


pytestmark = pytest.mark.build_infra

ROOT_DOCKERFILE = REPO_ROOT / 'Dockerfile'
EXPECTED_DOCKER = '/usr/local/bin/docker'
# The trusted compose files, spelled out (ADR tj-4rr0la; stack.COMPOSE_FILES is pinned to the Makefile
# in test_commands.py).
TRUSTED_COMPOSE_FILES = (
    'docker-compose.yaml',
    'docker-compose.test-client.yaml',
    'docker-compose.agent-stack.yaml',
    'docker-compose.fake.yaml',
)
# Addendum 15: the services a compose `run` can build, and the image-only ones stack_wipe clears.
LITERAL_BUILT_SERVICES = {'data_store', 'data_ingest', 'test_client'}
# The verbs that can build today, and the ones that must stay base-free (FINAL DESIGN note).
BUILDING_VERBS = {'stack_up', 'run_system_tests', 'migrate', 'migrate_status', 'seed_dump'}
BASE_FREE_VERBS = {'stack_down', 'stack_wipe', 'logs', 'ps'}


# --- the Dockerfile's external refs ----------------------------------------------------------------


def _dockerfile_external_refs() -> list[str]:
    """Every FROM image and COPY --from image of the root Dockerfile that is not one of its own stages."""
    lines = ROOT_DOCKERFILE.read_text(encoding='utf-8').replace('\\\n', ' ').splitlines()
    stages, named = set(), []
    for line in lines:
        words = shlex.split(line, comments=True)
        if words[:1] == ['FROM']:
            operands = [word for word in words[1:] if not word.startswith('--')]
            if len(operands) >= 3 and operands[1].upper() == 'AS':
                stages.add(operands[2])
            named.append(operands[0])
        elif words[:1] == ['COPY']:
            named += [word.split('=', 1)[1] for word in words[1:] if word.startswith('--from=')]
    return [ref for ref in named if ref not in stages and not ref.isdigit()]


def test_every_external_ref_in_the_root_dockerfile_is_digest_pinned():
    """Pin (1): FROM and COPY --from of a registry image carry @sha256:<64 hex>; stage names are not external."""
    refs = _dockerfile_external_refs()
    assert len(refs) >= 3, f'expected both debian FROMs and the uv COPY --from, parsed {refs}'
    assert 'base_build_image' not in refs, 'a stage name was taken for an external ref'
    unpinned = [ref for ref in refs if not re.search(r'@sha256:[0-9a-f]{64}$', ref)]
    assert unpinned == [], f'external refs without a digest: {unpinned}'


def test_base_images_is_exactly_the_dockerfiles_external_refs():
    """Pins (2) and (7): one constant, equal to the Dockerfile's set; the failure says what a bump must change."""
    in_dockerfile = set(_dockerfile_external_refs())
    assert len(stack.BASE_IMAGES) == len(set(stack.BASE_IMAGES)), f'a duplicate in BASE_IMAGES: {stack.BASE_IMAGES}'
    assert set(stack.BASE_IMAGES) == in_dockerfile, (
        f'the root Dockerfile names {sorted(in_dockerfile)} but tools/agent_mcp/stack.py BASE_IMAGES lists '
        f'{sorted(stack.BASE_IMAGES)}. A Dependabot digest bump (or any edit to a FROM / COPY --from '
        f'ref) must update stack.BASE_IMAGES in the same PR, ref for ref, as tag@sha256:<index digest> -- '
        f'otherwise the MCP ensures the old bases and the build resolves the new one from inside '
        f'agent_mcp (ADR tj-4rr0la addendum 14 (5)).'
    )


@pytest.mark.parametrize('ref', stack.BASE_IMAGES)
def test_every_base_image_is_a_tagged_index_ref(ref: str):
    """Pin (6): 'name:tag@sha256:<64 hex>' -- the tag kept before '@' -- and nothing else in the shape."""
    assert ref.count('@') == 1, ref
    name_and_tag, digest = ref.split('@')
    assert re.fullmatch(r'sha256:[0-9a-f]{64}', digest), ref
    name, _, tag = name_and_tag.rpartition(':')
    assert name and tag and '/' not in tag, f'{ref} drops its tag before the digest'


def test_the_ci_helpers_still_read_the_digest_pinned_lines():
    """Pin (5): UV_PIN_SOURCES reads the uv version off the pinned COPY, _dockerfile_stages the pinned FROMs."""
    uv_refs = [ref for ref in stack.BASE_IMAGES if ref.startswith('ghcr.io/astral-sh/uv:')]
    assert len(uv_refs) == 1, stack.BASE_IMAGES
    assert 'Dockerfile' in UV_PIN_SOURCES
    assert _uv_pins()['Dockerfile'] == uv_refs[0].split('@')[0].rpartition(':')[2]
    stages = _dockerfile_stages()
    for stage in ('base_build_image', 'base_deploy_image'):
        assert stage in stages, f'_dockerfile_stages lost {stage}: {sorted(stages)}'
        assert stages[stage][0] in stack.BASE_IMAGES, f'{stage} parses its parent as {stages[stage][0]!r}'


def test_built_services_are_exactly_the_services_with_a_build_key_in_the_trusted_files():
    """Pin (8): addendum 15's `run` rule rests on BUILT_SERVICES, so it must name every buildable service."""
    with_build = set()
    for name in TRUSTED_COMPOSE_FILES:
        services = yaml.safe_load((REPO_ROOT / name).read_text(encoding='utf-8')).get('services') or {}
        with_build |= {service for service, spec in services.items() if isinstance(spec, dict) and 'build' in spec}
    assert with_build == LITERAL_BUILT_SERVICES, with_build
    assert set(stack.BUILT_SERVICES) == with_build, (
        f'stack.BUILT_SERVICES {sorted(stack.BUILT_SERVICES)} vs the services with a build: key {sorted(with_build)}: '
        f'a `run` of a missing one would build with no bases ensured (ADR tj-4rr0la addendum 15)'
    )


# --- stack.builds: what CAN build ------------------------------------------------------------------

_CAN_BUILD = [
    ['build', 'data_store', 'data_ingest', 'test_client'],
    ['build'],
    ['run', '--rm', '--no-deps', '--build', 'test_client', 'tests/system'],
    ['up', '-d', '--wait'],
    ['up'],
    ['run', '--rm', '--no-deps', 'data_store', '/code/.venv/bin/alembic', 'current'],
    ['run', '--rm', 'data_ingest'],
    ['run', '--rm', '--no-deps', 'test_client', 'tests/system'],
    ['create', '--build'],
]
_CANNOT_BUILD = [
    ['run', '--rm', '--no-deps', '--user', '0', '--entrypoint', 'sh', 'postgres', '-c', 'x', 'clear', '/d'],
    ['run', '--rm', '--no-deps', '--user', '0', '--entrypoint', 'sh', 'kafka', '-c', 'x', 'clear', '/d'],
    ['ps', '--all'],
    ['ps', '-q', 'postgres'],
    ['logs', '--no-color', '--tail', '50', 'postgres'],
    ['logs', '--tail', '50', 'data_store'],
    ['down', '--remove-orphans'],
    [],
]


@pytest.mark.parametrize('tail', _CAN_BUILD, ids=' '.join)
def test_builds_is_true_for_every_compose_command_that_can_build(tail: list[str]):
    """Addendum 15: build, any --build, up always, and a run naming a BUILT_SERVICES service."""
    assert stack.builds(tail) is True


@pytest.mark.parametrize('tail', _CANNOT_BUILD, ids=lambda tail: ' '.join(tail) or 'empty')
def test_builds_is_false_for_image_only_runs_and_read_only_commands(tail: list[str]):
    """Addendum 15: stack_wipe's postgres/kafka clears, ps, logs (even of a built service), down."""
    assert stack.builds(tail) is False


# --- every builder flags its steps --------------------------------------------------------------

# A sample call of every public *_steps builder. A builder added to stack.py without an entry here
# goes red in the sweep below, so a later build verb (seed_dump, tj-irhy0a.22) is swept too.
_BUILDER_SAMPLES = {
    'stack_up_steps': (),
    'stack_down_steps': (),
    'wipe_clear_steps': (['postgres', 'kafka'],),
    'postgres_running_steps': (),
    'alembic_steps': (['upgrade', 'head'], ['current']),
    'system_tests_steps': (['tests/system'],),
    'seed_dump_steps': ('2026-01-02',),
    'logs_steps': ('data_store', 50),
    'ps_steps': (),
}


def _builders() -> dict[str, object]:
    return {
        name: value
        for name, value in vars(stack).items()
        if name.endswith('_steps') and not name.startswith('_') and inspect.isfunction(value)
    }


def _can_build(tail: list[str]) -> bool:
    """The design's rule (addendum 15), from literals: what a compose tail is required to be flagged as."""
    return bool(tail) and (
        tail[0] in ('build', 'up')
        or '--build' in tail
        or (tail[0] == 'run' and bool(LITERAL_BUILT_SERVICES & set(tail)))
    )


def test_every_steps_builder_flags_each_step_that_can_build():
    """Pin (3), builder half: every *_steps builder sets Step.builds by the addendum-15 rule on every step."""
    builders = _builders()
    assert set(builders) == set(_BUILDER_SAMPLES), (
        f'builders {sorted(builders)} vs samples {sorted(_BUILDER_SAMPLES)}: add the new builder here'
    )
    stack_dir, env_file = Path('/stack'), Path('/stack/agent_stack.env')
    flagged = 0
    for name, builder in builders.items():
        for step in builder(stack_dir, env_file, *_BUILDER_SAMPLES[name]):
            tail = list(step.argv[step_prefix_length() :])
            assert step.builds is _can_build(tail), f'{name}: {tail} has builds={step.builds}'
            flagged += step.builds
    assert flagged >= 5, 'the sweep found too few building steps to mean anything'


# --- every verb ensures the bases before it can build -----------------------------------------------


def _inspect(ref: str) -> tuple[str, ...]:
    return (EXPECTED_DOCKER, 'image', 'inspect', '--format', '{{.Id}}', ref)


def _pull(ref: str) -> tuple[str, ...]:
    return (EXPECTED_DOCKER, 'pull', ref)


def _is_base(argv: tuple[str, ...]) -> bool:
    return any(argv in (_inspect(ref), _pull(ref)) for ref in stack.BASE_IMAGES)


def _run_verb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, respond=default_response):
    rig = make_rig(tmp_path, monkeypatch, FakeDocker(respond=respond))
    record_state(rig.layout)
    populate_data(rig.layout)
    rig.docker.hook = clearing_hook(rig.layout)
    return rig, rig.call(verb, dict(VERB_SAMPLES[verb]))


def _bases_absent(step: stack.Step) -> runner.ProcessResult:
    if step.argv[1:3] == ('image', 'inspect'):
        return runner.ProcessResult(1, b'', b'Error: No such image')
    return default_response(step)


@pytest.mark.parametrize('respond', [default_response, _bases_absent], ids=['present', 'absent'])
@pytest.mark.parametrize('verb', sorted(set(VERB_SAMPLES) - DOCKERLESS_VERBS))
def test_every_step_that_can_build_is_preceded_by_the_ensure_bases_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, respond
):
    """Pin (3), verb half: every step that can build comes after an inspect of every BASE_IMAGES ref.

    Run through AgentStack, with the bases present and absent. A build, --build, up or run of a
    built service is preceded by the inspects; a verb that cannot build runs no base step at all
    (stack_wipe's postgres/kafka clears included); and no step anywhere passes --pull.
    """
    assert set(VERB_SAMPLES) - DOCKERLESS_VERBS == BUILDING_VERBS | BASE_FREE_VERBS, 'classify the new verb here'
    rig, result = _run_verb(tmp_path, monkeypatch, verb, respond)
    assert result['status'] == 'ok', result
    argvs = [step.argv for step in rig.docker.steps]
    assert not [argv for argv in argvs if any(word == '--pull' or word.startswith('--pull=') for word in argv)]
    building = [
        index
        for index, argv in enumerate(argvs)
        if argv[1] == 'compose' and _can_build(list(argv[step_prefix_length() :]))
    ]
    if verb in BASE_FREE_VERBS:
        assert building == [] and not [argv for argv in argvs if _is_base(argv)], argvs
        if verb == 'stack_wipe':
            runs = [argv[step_prefix_length() :] for argv in argvs if argv[step_prefix_length()] == 'run']
            assert {run[7] for run in runs} == {'postgres', 'kafka'}, 'the clears did not run, so nothing was shown'
        return
    assert building, f'{verb} ran no step that can build: {argvs}'
    for index in building:
        before = argvs[:index]
        missing = [ref for ref in stack.BASE_IMAGES if _inspect(ref) not in before]
        assert missing == [], f'{verb}: {shlex.join(argvs[index])} runs before the inspect of {missing}'
    for step in rig.docker.steps:
        if _is_base(step.argv):
            assert step.cwd == rig.layout.snapshot and step.builds is False, step


def test_the_bases_are_ensured_once_per_verb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Pin (3a): migrate_status runs ps, the inspects once, then both alembic runs -- no second inspect pair."""
    rig, result = _run_verb(tmp_path, monkeypatch, 'migrate_status')
    assert result['status'] == 'ok', result
    shape = [
        'inspect' if _is_base(step.argv) else ' '.join(step.argv[step_prefix_length() :][:1] + step.argv[-1:])
        for step in rig.docker.steps
    ]
    assert shape == ['ps postgres', *['inspect'] * len(stack.BASE_IMAGES), 'run current', 'run history'], shape


def test_a_base_is_pulled_only_when_its_inspect_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Addendum 14 (3): inspect each ref; pull exactly the absent one; then build and both up steps.

    Re-pinned by tj-zgq5v2: stack_up's plain `up` became two (the infrastructure, then the
    force-recreate of SNAPSHOT_BOUND_SERVICES), and the base steps still all come before the build.
    """
    absent = stack.BASE_IMAGES[0]

    def respond(step: stack.Step) -> runner.ProcessResult:
        return runner.ProcessResult(1 if step.argv == _inspect(absent) else 0, b'', b'')

    rig, result = _run_verb(tmp_path, monkeypatch, 'stack_up', respond)
    assert result['status'] == 'ok', result
    base_steps = [step.argv for step in rig.docker.steps if _is_base(step.argv)]
    assert base_steps == [_inspect(absent), _pull(absent), *(_inspect(ref) for ref in stack.BASE_IMAGES[1:])]
    assert [step.argv[step_prefix_length()] for step in rig.docker.steps if not _is_base(step.argv)] == [
        'build',
        'up',
        'up',
    ]
    last_base = max(index for index, step in enumerate(rig.docker.steps) if _is_base(step.argv))
    assert last_base < min(index for index, step in enumerate(rig.docker.steps) if not _is_base(step.argv))


@pytest.mark.parametrize('failing', range(len(stack.BASE_IMAGES)))
@pytest.mark.parametrize('verb', sorted(BUILDING_VERBS))
def test_a_failed_pull_stops_the_verb_naming_the_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, failing: int
):
    """Pin (3b), addendum 14 (e): inspect fails, pull fails -> 'failed', the ref named, nothing that builds after."""
    ref = stack.BASE_IMAGES[failing]

    def respond(step: stack.Step) -> runner.ProcessResult:
        if step.argv in (_inspect(ref), _pull(ref)):
            return runner.ProcessResult(1, b'', b'pull access denied')
        return default_response(step)

    rig, result = _run_verb(tmp_path, monkeypatch, verb, respond)
    assert result['status'] == 'failed', result
    assert ref in result['message'] and verb in result['message'], result['message']
    assert result['exit_status'] == 1
    argvs = [step.argv for step in rig.docker.steps]
    assert argvs[-1] == _pull(ref), f'{verb} ran on after the failed pull: {argvs}'
    assert [step['command'] for step in result['steps']][-1] == shlex.join(_pull(ref))
    assert not [argv for argv in argvs if argv[1] == 'compose' and _can_build(list(argv[step_prefix_length() :]))]
    assert not [other for other in stack.BASE_IMAGES[failing + 1 :] if _inspect(other) in argvs]


def test_any_step_flagged_builds_gets_the_bases_first_whatever_produced_it(tmp_path: Path):
    """Addendum 14 (3), 'any later verb inherits the rule': the runner's one choke point, not each verb.

    A Step built by hand -- as a future builder would -- with builds=True is preceded by the inspects;
    one with builds=False is not; and the second building step of the same call adds none.
    """
    seen: list[stack.Step] = []

    async def run(step: stack.Step) -> runner.ProcessResult:
        seen.append(step)
        return runner.ProcessResult(0, b'', b'')

    async def drive() -> None:
        call = runner._Call(run)
        await call.step(stack.Step(('/usr/local/bin/docker', 'compose', 'plain'), tmp_path))
        await call.step(stack.Step(('/usr/local/bin/docker', 'compose', 'first'), tmp_path, builds=True))
        await call.step(stack.Step(('/usr/local/bin/docker', 'compose', 'second'), tmp_path, builds=True))

    asyncio.run(drive())
    assert [step.argv for step in seen] == [
        ('/usr/local/bin/docker', 'compose', 'plain'),
        *(_inspect(ref) for ref in stack.BASE_IMAGES),
        ('/usr/local/bin/docker', 'compose', 'first'),
        ('/usr/local/bin/docker', 'compose', 'second'),
    ]
    assert all(step.cwd == tmp_path for step in seen)


def test_base_pull_failed_names_its_ref():
    exception = runner.BasePullFailed('example:1@sha256:' + '0' * 64)
    assert exception.ref == 'example:1@sha256:' + '0' * 64 and exception.ref in str(exception)


def test_no_source_line_in_the_mcp_passes_pull():
    """Addendum 14 (c): nothing in stack.py or runner.py spells --pull as an argv word."""
    offenders = []
    for name in ('stack.py', 'runner.py'):
        tree = ast.parse((REPO_ROOT / 'tools' / 'agent_mcp' / name).read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and (node.value == '--pull' or node.value.startswith('--pull='))
            ):
                offenders.append(f'{name}:{node.lineno}')
    assert offenders == [], offenders
