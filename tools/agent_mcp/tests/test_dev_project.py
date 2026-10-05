"""The dev pair's CONTAINMENT: one project, two read-only subcommands, and no way to name anything else.

tj-tq2hn6 (B2), covering tj-kzy7w2 (B1). Design: ADR tj-4rr0la addendum 18, with the corrections of
addendum 19. The architect's six required assertions at B1's gate are the spine of this module and
each one names itself below: R1 check_dev_argv refuses, R2 validate_dev_service refuses, R3 the enum
MEMBERSHIP, R4 the closed schema (widened in test_runner.py, where that sweep lives), R5 the attached
short option, R6 prod is not nameable.

WHAT THIS MODULE IS NOT. test_commands.py:224 already pins each dev verb's WHOLE argv from literals
and was reddened six ways (DEV_PROJECT retargeted, the prefix growing a -f, the prefix growing an
--env-file, dev_ps becoming a mutating `down`, dev_logs growing --follow). The END STATE is covered
there. What is covered HERE is the GUARD layer -- the three refusals a mutation could gut with the
whole suite staying green, which is the definition of an unguarded guard:
    check_dev_argv        gutted to a bare `return`              -> 3005 green
    validate_dev_service  its enum check gutted                  -> 3005 green
    DEV_SERVICES          widened with an arbitrary service name  -> 3005 green

EVERY CONSTANT IS SPELLED FROM LITERALS HERE, never read from stack.py. A test that read
stack.DEV_PROJECT or stack.DEV_SERVICES would agree with any change to them, and those constants ARE
the safety case: the whole argument for letting an agent reach the user's own compose project at all
is that the project is one fixed name and the subcommands are two read-only ones.

NO DOCKER RUNS, here or anywhere in this suite (tj-j4wknb R4). What is pinned is the argv the server
hands its runner, with forbid_real_subprocesses() making a bypass fail loudly. Neither dev verb has
ever run against a real daemon -- there is no docker CLI in the devcontainer, tools/agent_mcp is
outside stack.SNAPSHOT_SOURCES so a stack_up does not exercise it, and the live server runs from the
baked /opt/agent_mcp. ADR tj-4rr0la addendum 18 clause (7) holds that open against a host sitting;
nothing in this module claims to close it.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from tools.agent_mcp import runner, stack
from tools.agent_mcp.tests.harness import FakeDocker, forbid_real_subprocesses, make_rig, record_state


pytestmark = pytest.mark.build_infra

# --- the constants, from literals --------------------------------------------------------------
# The user's own dev compose project: the checkout directory's name, which is what the Makefile's
# DEV_COMPOSE targets get from compose's default because they pass no -p.
EXPECTED_DEV_PROJECT = 'trader_joe'
# The agent stack's OWN project, spelled here so a test can assert the dev verbs cannot reach it
# either. These verbs reach one project and it is not this one.
EXPECTED_AGENT_PROJECT = 'trader_joe_agent_stack'
EXPECTED_DOCKER = '/usr/local/bin/docker'
EXPECTED_DEV_PREFIX = [EXPECTED_DOCKER, 'compose', '-p', EXPECTED_DEV_PROJECT]
# R3: the closed service enum, by membership. pgAdmin is deliberately absent -- it lives in
# docker-compose.tools.yaml, a file no verb loads.
EXPECTED_DEV_SERVICES = ('postgres', 'data_store', 'data_ingest')
# The only two subcommands a dev verb may spell, both read-only.
EXPECTED_DEV_READ_COMMANDS = {'ps', 'logs'}
# The options that would point a dev verb at content, another project or an env file.
EXPECTED_DEV_FORBIDDEN_OPTIONS = {
    '-f',
    '--file',
    '-p',
    '--project-name',
    '--project-directory',
    '--env-file',
    '--profile',
}
# The agent stack's logs clamp, which the dev pair must share rather than invent a second rule.
EXPECTED_TAIL_MAX = 2000
EXPECTED_TAIL_DEFAULT = 200
# Both dev budgets are their read-only siblings': the same two compose calls.
EXPECTED_DEV_TIMEOUT_SECONDS = 60

DEV_VERBS = ('dev_ps', 'dev_logs')


def _dev_steps(rig) -> list[list[str]]:
    return [list(step.argv) for step in rig.docker.steps]


def _words(rig) -> set[str]:
    return {word for step in rig.docker.steps for word in step.argv}


# ===============================================================================================
# R3. THE MEMBERSHIP OF EVERY CLOSED SET.
#
# A DIFFERENT TEST from a refusal test, and the one the builder did not declare: widening
# DEV_SERVICES with an arbitrary name left all 3005 tests green, so the closed enum could GROW in
# silence. A refusal test proves the gate fires on what is outside the set; only a membership test
# proves what the set IS. Both are needed, and neither substitutes for the other.
# ===============================================================================================


def test_the_dev_project_is_exactly_one_fixed_name():
    """R6's foundation: one name, a module constant, never derived from anything an agent writes."""
    assert stack.DEV_PROJECT == EXPECTED_DEV_PROJECT
    assert stack.dev_compose_prefix() == EXPECTED_DEV_PREFIX, 'the dev prefix is four words and nothing more'
    assert stack.DEV_PROJECT != EXPECTED_AGENT_PROJECT, 'the dev verbs must not share the agent stack project'


def test_the_dev_service_enum_holds_exactly_these_three_names():
    """R3: DEV_SERVICES widened with an arbitrary name left the whole suite green. Pinned by membership."""
    assert tuple(stack.DEV_SERVICES) == EXPECTED_DEV_SERVICES, (
        "DEV_SERVICES is the set of services an agent may name in the user's own project; "
        'growing it is a widening of agent reach and needs the ADR addendum that allowing it would need'
    )


def test_the_advertised_dev_logs_enum_is_the_same_three_names():
    """The schema an MCP client reads and the set the server enforces must not drift apart."""
    assert runner.VERB_SCHEMAS['dev_logs']['properties']['service']['enum'] == list(EXPECTED_DEV_SERVICES)


def test_only_two_read_only_subcommands_are_spellable():
    """R3, the other half: DEV_READ_COMMANDS growing an `up` or a `down` is the worst outcome here."""
    assert set(stack.DEV_READ_COMMANDS) == EXPECTED_DEV_READ_COMMANDS
    for mutating in ('up', 'down', 'run', 'exec', 'build', 'rm', 'kill', 'restart', 'start', 'stop', 'cp', 'pull'):
        assert mutating not in stack.DEV_READ_COMMANDS, f'{mutating} is spellable by a dev verb'


def test_the_forbidden_option_set_holds_every_steering_option():
    """A shrink here silently re-opens a steering option, and nothing else in the suite reads this set."""
    assert set(stack.DEV_FORBIDDEN_OPTIONS) == EXPECTED_DEV_FORBIDDEN_OPTIONS


def test_the_attached_short_prefixes_are_exactly_the_short_members_of_that_set():
    """tj-v4e9ke: spelled from a literal, and the derivation pinned so it cannot start matching a long one.

    A long option would be a disaster here rather than a widening of the right kind: '--tail' is a
    legitimate dev word, and a prefix rule that caught '--t...' would refuse the verb's own argv.
    """
    assert stack.DEV_FORBIDDEN_SHORT_PREFIXES == ('-f', '-p')
    assert set(stack.DEV_FORBIDDEN_SHORT_PREFIXES) <= set(stack.DEV_FORBIDDEN_OPTIONS)
    assert not [prefix for prefix in stack.DEV_FORBIDDEN_SHORT_PREFIXES if prefix.startswith('--')]


def test_both_dev_verbs_carry_a_timeout_budget():
    """The bead's 'timeout budgets exist for both', from a literal rather than from the sibling's entry."""
    for verb in DEV_VERBS:
        assert runner.VERB_TIMEOUT_SECONDS[verb] == EXPECTED_DEV_TIMEOUT_SECONDS, verb
    assert runner.VERB_TIMEOUT_SECONDS['logs'] == runner.VERB_TIMEOUT_SECONDS['dev_logs']
    assert runner.VERB_TIMEOUT_SECONDS['ps'] == runner.VERB_TIMEOUT_SECONDS['dev_ps']


# ===============================================================================================
# R1. check_dev_argv REFUSES.
#
# Gutted to a bare `return`, nothing in the suite noticed: every tail the builders produce is valid,
# so a check that never fires is indistinguishable from a check that was deleted -- until a future
# caller builds a different argv, which is the one case the function exists for.
# ===============================================================================================

_BAD_PREFIXES = {
    'another project': [EXPECTED_DOCKER, 'compose', '-p', 'someone_elses_stack', 'ps'],
    'the agent stack project': [EXPECTED_DOCKER, 'compose', '-p', EXPECTED_AGENT_PROJECT, 'ps'],
    'the dev project as a prefix of a longer name': [EXPECTED_DOCKER, 'compose', '-p', 'trader_joe_prod', 'ps'],
    'no project at all': [EXPECTED_DOCKER, 'compose', 'ps'],
    'the long project option': [EXPECTED_DOCKER, 'compose', '--project-name', EXPECTED_DEV_PROJECT, 'ps'],
    'another binary': ['/usr/bin/docker', 'compose', '-p', EXPECTED_DEV_PROJECT, 'ps'],
    'a bare binary name': ['docker', 'compose', '-p', EXPECTED_DEV_PROJECT, 'ps'],
    'docker-compose v1': [EXPECTED_DOCKER, '-p', EXPECTED_DEV_PROJECT, 'ps'],
    'a file before the project': [EXPECTED_DOCKER, 'compose', '-f', 'x.yaml', '-p', EXPECTED_DEV_PROJECT, 'ps'],
    'not compose at all': [EXPECTED_DOCKER, 'run', '-p', EXPECTED_DEV_PROJECT, 'ps'],
    'empty': [],
    'the prefix alone, with no subcommand': list(EXPECTED_DEV_PREFIX),
}

# Every compose subcommand that creates, changes or removes something. `logs` and `ps` are the only
# two that may appear, so each of these must be refused by the subcommand check.
_MUTATING_SUBCOMMANDS = (
    'up',
    'down',
    'run',
    'exec',
    'build',
    'create',
    'start',
    'stop',
    'restart',
    'kill',
    'rm',
    'cp',
    'pull',
    'push',
    'scale',
    'pause',
    'port',
    'wait',
    'watch',
    'config',
    'version',
)


@pytest.mark.parametrize('argv', list(_BAD_PREFIXES.values()), ids=list(_BAD_PREFIXES))
def test_check_dev_argv_refuses_any_argv_whose_prefix_is_not_the_four_fixed_words(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
):
    """R1: the prefix IS the containment, so anything but those exact four words is refused."""
    forbid_real_subprocesses(monkeypatch)
    with pytest.raises(stack.Refused):
        stack.check_dev_argv(argv)


@pytest.mark.parametrize('subcommand', _MUTATING_SUBCOMMANDS)
def test_check_dev_argv_refuses_every_subcommand_outside_the_two_read_only_ones(
    monkeypatch: pytest.MonkeyPatch, subcommand: str
):
    """R1: no mutating command is constructible -- asserted over the words, not over the builders' intent."""
    forbid_real_subprocesses(monkeypatch)
    with pytest.raises(stack.Refused, match='read-only'):
        stack.check_dev_argv([*EXPECTED_DEV_PREFIX, subcommand])


@pytest.mark.parametrize('option', sorted(EXPECTED_DEV_FORBIDDEN_OPTIONS))
@pytest.mark.parametrize('subcommand', sorted(EXPECTED_DEV_READ_COMMANDS))
def test_check_dev_argv_refuses_every_steering_option_after_the_prefix(
    monkeypatch: pytest.MonkeyPatch, option: str, subcommand: str
):
    """R1: each member of DEV_FORBIDDEN_OPTIONS, separated and attached-long, under each read verb.

    Attached-long (`--env-file=x`) is caught because the scan splits on the first `=`. The attached
    SHORT form has its own pin below (R5, tj-v4e9ke), closed by the prefix arm of the same scan.
    """
    forbid_real_subprocesses(monkeypatch)
    with pytest.raises(stack.Refused, match='names no compose file, project or env file'):
        stack.check_dev_argv([*EXPECTED_DEV_PREFIX, subcommand, option, 'value'])
    with pytest.raises(stack.Refused, match='names no compose file, project or env file'):
        stack.check_dev_argv([*EXPECTED_DEV_PREFIX, subcommand, f'{option}=value'])


def test_check_dev_argv_refuses_a_second_project_option_after_the_prefix():
    """The prefix's own -p must stay the only one: a second would win and retarget the verb."""
    with pytest.raises(stack.Refused, match='names no compose file, project or env file'):
        stack.check_dev_argv([*EXPECTED_DEV_PREFIX, 'ps', '-p', EXPECTED_AGENT_PROJECT])


def test_check_dev_argv_accepts_exactly_the_two_argvs_the_builders_produce():
    """The other side of the gate: a check that refused everything would also pass every test above."""
    stack.check_dev_argv([*EXPECTED_DEV_PREFIX, 'ps', '--all'])
    stack.check_dev_argv([*EXPECTED_DEV_PREFIX, 'logs', '--no-color', '--tail', '50', 'postgres'])


def test_the_dev_step_builders_run_check_dev_argv_on_the_argv_they_hand_the_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """R1's wiring: the call site, not the function. A change that dropped it is caught here.

    check_dev_argv is replaced by a spy that records and refuses. Both verbs must then answer
    'refused' having run NO docker step -- which is only possible if the check runs inside the step
    builder, ahead of the subprocess, on the real argv.
    """
    seen: list[list[str]] = []

    def spy(argv):
        seen.append(list(argv))
        raise stack.Refused('spy refusal')

    monkeypatch.setattr(stack, 'check_dev_argv', spy)
    for verb, arguments in (('dev_ps', {}), ('dev_logs', {'service': 'postgres', 'tail': 50})):
        seen.clear()
        rig = make_rig(tmp_path / verb, monkeypatch, FakeDocker())
        result = rig.call(verb, arguments)
        assert result['status'] == 'refused' and result['message'] == 'spy refusal', result
        assert rig.docker.steps == [], f'{verb} reached docker with an argv check_dev_argv had refused'
        assert seen and all(argv[:4] == EXPECTED_DEV_PREFIX for argv in seen), seen
        assert len(rig.audit_lines()) == 1, 'a refused dev call must still be audited'


# ===============================================================================================
# R2. validate_dev_service REFUSES, and it is the SOLE enforcement of the enum.
#
# NOT defence in depth, which is why its gutting was invisible: runner._check_arguments
# (runner.py:299-310) validates only unknown and missing KEYS and never applies the schema's `type`
# or its `enum`. The advertised enum constrains a well-behaved MCP client and nothing else, so this
# one function is the whole gate (ADR tj-4rr0la addendum 19 (C)).
# ===============================================================================================

_BAD_SERVICES = {
    'outside the enum': 'redis',
    'a service of another compose file': 'pgadmin',
    'a container name rather than a service': 'trader_joe-postgres-1',
    'the agent stack project name': EXPECTED_AGENT_PROJECT,
    'a shell tail': 'postgres; rm -rf /',
    'a shell substitution': '$(cat /etc/passwd)',
    'a compose option': '--follow',
    'an attached project option': f'-p{EXPECTED_AGENT_PROJECT}',
    'a path': '/etc/passwd',
    'a relative path': '../postgres',
    'wrong case': 'POSTGRES',
    'trailing whitespace': 'postgres ',
    'a prefix of a member': 'post',
    'a member with a suffix': 'postgres1',
    'empty': '',
    'a nul': 'postgres\0',
    'a newline': 'postgres\ndata_store',
    'an integer': 5,
    'a bool': True,
    'none': None,
    'a list of members': ['postgres'],
    'a dict': {'service': 'postgres'},
}


@pytest.mark.parametrize('service', list(_BAD_SERVICES.values()), ids=list(_BAD_SERVICES))
def test_validate_dev_service_refuses_anything_outside_the_enum(monkeypatch: pytest.MonkeyPatch, service: object):
    """R2, at the function: the sole enforcement of the service enum, so a gutted check is a gate removed."""
    forbid_real_subprocesses(monkeypatch)
    with pytest.raises(stack.Refused, match='service must be one of'):
        stack.validate_dev_service(service)


@pytest.mark.parametrize('service', EXPECTED_DEV_SERVICES)
def test_validate_dev_service_accepts_each_member_unchanged(service: str):
    assert stack.validate_dev_service(service) == service


@pytest.mark.parametrize('service', list(_BAD_SERVICES.values()), ids=list(_BAD_SERVICES))
def test_dev_logs_refuses_a_service_outside_the_enum_before_docker_is_reached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, service: object
):
    """R2, through the PUBLIC call path: a future change that drops the call site is caught here.

    runner._check_arguments never applies the schema's enum, so if _dev_logs stopped calling
    validate_dev_service the string would flow straight into the argv and this call would answer
    'ok'. The refusal must be audited and no subprocess may run.
    """
    rig = make_rig(tmp_path, monkeypatch)
    result = rig.call('dev_logs', {'service': service, 'tail': 50})
    assert result['status'] == 'refused' and 'service must be one of' in result['message'], result
    assert rig.docker.steps == [], f'{service!r} reached docker'
    audit = json.loads(rig.audit_lines()[-1])
    assert audit['verb'] == 'dev_logs' and audit['status'] == 'refused', audit
    assert audit['arguments'] is None, 'a refused argument reached the audit log'


def test_the_two_service_enums_are_separate_objects_so_widening_one_cannot_widen_the_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The agent stack's SERVICES and the dev project's DEV_SERVICES hold the same names by accident today.

    They are kept apart so that adding a service to one stack cannot silently widen what the other's
    verb accepts. Widening SERVICES here must leave dev_logs refusing the new name.
    """
    monkeypatch.setattr(stack, 'SERVICES', (*stack.SERVICES, 'pgadmin'))
    rig = make_rig(tmp_path, monkeypatch)
    result = rig.call('dev_logs', {'service': 'pgadmin', 'tail': 50})
    assert result['status'] == 'refused', result
    assert rig.docker.steps == []


# ===============================================================================================
# R6. PROD IS NOT NAMEABLE, as its own assertion rather than as a consequence of R1.
#
# Two independent statements: neither schema HAS a field that could carry a project, a file or a
# command; and no accepted value of the fields they do have changes any word of the argv but the
# service name and the tail count.
# ===============================================================================================

# Field names that would hand an agent a project, a file, a command or a directory. None may appear
# in either dev schema -- the point is that the reach is not parameterised at all.
_FORBIDDEN_SCHEMA_FIELDS = (
    'project',
    'project_name',
    'projectName',
    'p',
    'file',
    'files',
    'compose_file',
    'f',
    'env_file',
    'profile',
    'command',
    'subcommand',
    'args',
    'argv',
    'options',
    'flags',
    'worktree',
    'paths',
    'path',
    'directory',
    'project_directory',
    'follow',
)


def test_neither_dev_schema_has_a_project_a_file_or_a_command_field():
    """R6: the project is not an argument because there is no argument that could carry it."""
    assert set(runner.VERB_SCHEMAS['dev_ps']['properties']) == set(), 'dev_ps takes no argument at all'
    assert set(runner.VERB_SCHEMAS['dev_logs']['properties']) == {'service', 'tail'}
    for verb in DEV_VERBS:
        schema = runner.VERB_SCHEMAS[verb]
        assert schema['additionalProperties'] is False and schema['type'] == 'object', verb
        present = [field for field in _FORBIDDEN_SCHEMA_FIELDS if field in schema['properties']]
        assert not present, f'{verb} advertises {present}, which could steer compose'
    assert runner.VERB_SCHEMAS['dev_logs']['required'] == ['service'], 'service is not optional'


# Every argument either verb accepts: the whole cross product of the enum and the clamp's interesting
# values, plus dev_logs with `tail` omitted (its default). If the project were reachable from an
# argument at all, it would be reachable from one of these.
_ACCEPTED_CALLS: dict[str, tuple[str, dict[str, Any]]] = {
    'dev_ps': ('dev_ps', {}),
    'dev_logs default tail': ('dev_logs', {'service': 'postgres'}),
    **{
        f'dev_logs {service} tail={tail}': ('dev_logs', {'service': service, 'tail': tail})
        for service in EXPECTED_DEV_SERVICES
        for tail in (-(10**9), -1, 0, 1, 50, EXPECTED_TAIL_DEFAULT, EXPECTED_TAIL_MAX, EXPECTED_TAIL_MAX + 1, 10**9)
    },
}

# Everything a dev verb may ever hand the daemon, spelled out. A new word appearing in any argv --
# an option, a path, another project -- goes red here even if it is read-only.
_PERMITTED_WORDS = {
    EXPECTED_DOCKER,
    'compose',
    '-p',
    EXPECTED_DEV_PROJECT,
    'ps',
    '--all',
    'logs',
    '--no-color',
    '--tail',
    *EXPECTED_DEV_SERVICES,
}


@pytest.mark.parametrize(('verb', 'arguments'), list(_ACCEPTED_CALLS.values()), ids=list(_ACCEPTED_CALLS))
def test_no_accepted_argument_changes_the_project_or_adds_a_word(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, arguments: dict[str, Any]
):
    """R6: over every accepted call, the project word is the fixed one and no new word appears.

    The only words that may vary with an argument are the service name and the tail count. The
    project is word four of every argv and it is the dev project, never the agent stack's and never
    anything an argument spelled.
    """
    rig = make_rig(tmp_path, monkeypatch)
    result = rig.call(verb, dict(arguments))
    assert result['status'] == 'ok', result
    argvs = _dev_steps(rig)
    assert argvs, f'{verb} ran no docker step, so there is nothing to pin'
    for argv in argvs:
        assert argv[:4] == EXPECTED_DEV_PREFIX, argv
        assert argv.count('-p') == 1, f'more than one project option: {argv}'
        assert argv[4] in EXPECTED_DEV_READ_COMMANDS, f'{argv[4]} is not a read-only subcommand'
        assert not [word for word in argv[5:] if word.split('=', 1)[0] in EXPECTED_DEV_FORBIDDEN_OPTIONS], argv
    extra = {word for word in _words(rig) if word not in _PERMITTED_WORDS and not word.isdigit()}
    assert not extra, f'{verb} handed the daemon {sorted(extra)}, which is outside the permitted vocabulary'
    assert EXPECTED_AGENT_PROJECT not in _words(rig), 'a dev verb reached the agent stack project'


@pytest.mark.parametrize(('verb', 'arguments'), list(_ACCEPTED_CALLS.values()), ids=list(_ACCEPTED_CALLS))
def test_every_accepted_dev_argv_also_passes_check_dev_argv_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, arguments: dict[str, Any]
):
    """The two guards agree: whatever the builders produce is also what the re-check accepts."""
    rig = make_rig(tmp_path, monkeypatch)
    assert rig.call(verb, dict(arguments))['status'] == 'ok'
    for argv in _dev_steps(rig):
        stack.check_dev_argv(argv)


def test_the_dev_verbs_run_outside_the_repository_and_name_no_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """No compose file, no project directory, no env file -- so no worktree path may appear, cwd included.

    cwd is the stack directory: outside the repository, holding no compose file and no `.env`, so a
    compose that tried to DISCOVER a project would find nothing. The user's root .env, whose read
    guard this epic must not defeat, is never named and never reachable by discovery.
    """
    rig = make_rig(tmp_path, monkeypatch)
    for verb, arguments in (('dev_ps', {}), ('dev_logs', {'service': 'postgres', 'tail': 50})):
        rig.docker.steps.clear()
        assert rig.call(verb, dict(arguments))['status'] == 'ok'
        for step in rig.docker.steps:
            assert step.cwd == rig.layout.stack_dir, f'{verb} runs with cwd {step.cwd}'
            named = [word for word in step.argv if str(rig.layout.repo) in word or str(rig.layout.worktree) in word]
            assert not named, f'{verb} hands the daemon a repository path: {named}'
            assert not [word for word in step.argv if word.endswith(('.yaml', '.yml', '.env'))], list(step.argv)


def test_the_agent_stack_verbs_still_target_the_agent_stack_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The worst available outcome is a change here that quietly RETARGETS an existing verb.

    test_commands.py pins the agent stack's prefix in full; this is the cross-check from the other
    side -- the agent stack's own read-only siblings must not drift onto the dev project.
    """
    rig = make_rig(tmp_path, monkeypatch)
    record_state(rig.layout)
    for verb, arguments in (('ps', {}), ('logs', {'service': 'postgres', 'tail': 50})):
        rig.docker.steps.clear()
        assert rig.call(verb, dict(arguments))['status'] == 'ok'
        for step in rig.docker.steps:
            argv = list(step.argv)
            assert argv[:4] == [EXPECTED_DOCKER, 'compose', '-p', EXPECTED_AGENT_PROJECT], argv
            assert EXPECTED_DEV_PROJECT not in argv, f'{verb} reached the dev project: {argv}'


# ===============================================================================================
# THE CLAMP AND THE AUDIT, from the bead body.
# ===============================================================================================


@pytest.mark.parametrize(
    ('given', 'expected'),
    [
        (-(10**9), 1),
        (-1, 1),
        (0, 1),
        (1, 1),
        (50, 50),
        (EXPECTED_TAIL_DEFAULT, EXPECTED_TAIL_DEFAULT),
        (EXPECTED_TAIL_MAX, EXPECTED_TAIL_MAX),
        (EXPECTED_TAIL_MAX + 1, EXPECTED_TAIL_MAX),
        (10**9, EXPECTED_TAIL_MAX),
    ],
)
def test_the_dev_tail_is_clamped_at_both_ends_by_the_same_rule_as_the_agent_stack_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, given: int, expected: int
):
    """Clamped at BOTH ends, and by the EXISTING rule rather than a second one.

    The two verbs are run with the same `tail` and their --tail words compared, so a dev verb that
    grew its own clamp would go red even if that clamp also happened to be 1..2000.
    """
    rig = make_rig(tmp_path, monkeypatch)
    assert rig.call('dev_logs', {'service': 'postgres', 'tail': given})['status'] == 'ok'
    dev_argv = _dev_steps(rig)[-1]
    assert dev_argv[dev_argv.index('--tail') + 1] == str(expected), dev_argv

    rig.docker.steps.clear()
    assert rig.call('logs', {'service': 'postgres', 'tail': given})['status'] == 'ok'
    sibling = _dev_steps(rig)[-1]
    assert sibling[sibling.index('--tail') + 1] == dev_argv[dev_argv.index('--tail') + 1], (
        'the dev clamp and the agent stack clamp disagree, so one of them is a second rule'
    )


@pytest.mark.parametrize('tail', ['10', True, 1.0, None, [10], {'tail': 10}])
def test_a_dev_tail_that_is_not_an_integer_is_refused_before_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tail: object
):
    """The schema's `type` is never applied by _check_arguments, so clamp_tail is the only gate here too."""
    rig = make_rig(tmp_path, monkeypatch)
    result = rig.call('dev_logs', {'service': 'postgres', 'tail': tail})
    assert result['status'] == 'refused' and 'tail must be an integer' in result['message'], result
    assert rig.docker.steps == []


def test_dev_logs_omitting_tail_uses_the_shared_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = make_rig(tmp_path, monkeypatch)
    assert rig.call('dev_logs', {'service': 'postgres'})['status'] == 'ok'
    argv = _dev_steps(rig)[-1]
    assert argv[argv.index('--tail') + 1] == str(EXPECTED_TAIL_DEFAULT), argv


@pytest.mark.parametrize(('verb', 'arguments'), [('dev_ps', {}), ('dev_logs', {'service': 'postgres', 'tail': 50})])
def test_an_accepted_dev_call_is_audited_with_its_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, arguments: dict[str, Any]
):
    """Every call is audited: reaching the user's own project is exactly what a reader of the log wants to see."""
    rig = make_rig(tmp_path, monkeypatch)
    assert rig.call(verb, dict(arguments))['status'] == 'ok'
    lines = rig.audit_lines()
    assert len(lines) == 1, lines
    audit = json.loads(lines[-1])
    assert audit['verb'] == verb and audit['known'] is True and audit['status'] == 'ok', audit
    expected_arguments = {'tail': EXPECTED_TAIL_DEFAULT, **arguments} if verb == 'dev_logs' else {}
    assert audit['arguments'] == expected_arguments, audit


@pytest.mark.parametrize(
    ('verb', 'arguments'),
    [
        ('dev_logs', {'service': 'redis'}),
        ('dev_logs', {}),
        ('dev_logs', {'service': 'postgres', 'bogus': 1}),
        ('dev_ps', {'service': 'postgres'}),
        ('dev_ps', 'ps --all'),
    ],
    ids=[
        'service outside the enum',
        'missing service',
        'unknown keyword',
        'argument to an argumentless verb',
        'positional',
    ],
)
def test_a_refused_dev_call_is_audited_too_with_no_arguments_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, arguments: object
):
    """R4's companion: refusals are audited, and a refused argument never reaches the log."""
    rig = make_rig(tmp_path, monkeypatch)
    result = rig.call(verb, arguments)
    assert result['status'] == 'refused', result
    assert rig.docker.steps == []
    lines = rig.audit_lines()
    assert len(lines) == 1, lines
    audit = json.loads(lines[-1])
    assert audit['verb'] == verb and audit['status'] == 'refused' and audit['arguments'] is None, audit


# ===============================================================================================
# R5. THE ATTACHED SHORT OPTION -- THE HOLE IS CLOSED, AND THIS IS THE PIN THAT KEEPS IT CLOSED.
#
# check_dev_argv's scan WAS `word.split('=', 1)[0] in DEV_FORBIDDEN_OPTIONS` alone, so `-f x` and
# `--env-file=x` were refused but `-fFILE` and `-pNAME` were not, and docker's CLI accepts an
# attached short option value. tj-v4e9ke added the second arm,
# `word.startswith(DEV_FORBIDDEN_SHORT_PREFIXES)`, and these cases are its pin.
#
# IT WAS UNREACHABLE while it was open -- every dev tail is built from literals, and the only
# agent-supplied words are an enum-checked service and a clamped integer rendered with str() -- and
# the two tests below keep both halves nailed down: the first that the guard itself refuses the
# word, the second that nothing can hand it one anyway. check_dev_argv exists as the re-check for a
# FUTURE caller building a different argv, and for that caller the first half is the live one.
#
# HOW IT GOT HERE, because the mechanism is worth keeping: the architect's R5 ruling was xfail-and-
# name, or close it and pin it closed -- not unnamed either way. tj-tq2hn6 took the xfail arm for
# scope (closing it edits builder-shared's production code, and a validator that patches the code
# under review has stopped reviewing it) with strict=True, so when tj-v4e9ke closed the hole the
# five cases failed as XPASS(strict) and had to be flipped in the same commit. They were. The hole
# could not be closed and left undocumented, which is exactly what strict=True was there for.
# ===============================================================================================

_ATTACHED_SHORT_OPTIONS = (
    '-fdocker-compose.yaml',
    '-f/workspace/docker-compose.yaml',
    f'-p{EXPECTED_AGENT_PROJECT}',
    '-ptrader_joe_prod',
    '-p/etc',
)


@pytest.mark.parametrize('word', _ATTACHED_SHORT_OPTIONS)
def test_an_attached_short_option_is_refused(monkeypatch: pytest.MonkeyPatch, word: str):
    """R5, tj-v4e9ke: `-fFILE` and `-pNAME` are refused by the same message as the separated form."""
    forbid_real_subprocesses(monkeypatch)
    with pytest.raises(stack.Refused, match='names no compose file, project or env file'):
        stack.check_dev_argv([*EXPECTED_DEV_PREFIX, 'logs', word, 'postgres'])


@pytest.mark.parametrize('word', _ATTACHED_SHORT_OPTIONS)
def test_an_attached_short_option_is_not_reachable_from_any_accepted_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, word: str
):
    """The outer half of R5, kept now that the guard itself refuses these: defence in depth.

    The only argument that becomes an argv word verbatim is `service`, and the enum refuses each of
    these before check_dev_argv is ever reached. The test above pins the guard, this pins that
    nothing can hand it one of these words in the first place -- so neither layer going quiet is
    silent.
    """
    rig = make_rig(tmp_path, monkeypatch)
    result = rig.call('dev_logs', {'service': word, 'tail': 50})
    assert result['status'] == 'refused', result
    assert rig.docker.steps == [], f'{word} reached the daemon'
