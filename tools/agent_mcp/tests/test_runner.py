"""AgentStack: validation before any subprocess, one verb at a time, timeouts, output, the audit log.

tj-c4mosr.5 body bullets 2 and 6, items (5) as replaced, G3, G4, L, (7), (8), (9), A, S5 (and its
companion), S6. Design: ADR tj-4rr0la section 3, 5(a), addenda 5 and 6 (D4, D8, D9, D10).

Timings are measured INSIDE one running event loop (the validator's 02:14 note): asyncio.run() waits
for the default executor at shutdown, so a wrapper around it would report a blocked git thread's
whole sleep instead of when the verb answered.
"""

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from tools.agent_mcp import runner, stack
from tools.agent_mcp.tests.harness import (
    DEV_PROJECT_VERBS,
    LIVE_ENV_SENTINEL,
    VERB_SAMPLES,
    WORKTREE_NAME,
    FakeDocker,
    make_rig,
    record_state,
    step_prefix_length,
    tree_digest,
)


pytestmark = pytest.mark.build_infra


def _no_subprocess(rig, *, git_list_calls: int = 0) -> None:
    assert rig.docker.steps == [], f'docker ran: {rig.docker.steps}'
    assert rig.git.calls == [('worktree', 'list', '--porcelain')] * git_list_calls, rig.git.calls


# --- refused before any subprocess -------------------------------------------------------------

_BAD_NAMES = ['../x', '/abs', '', 'a b', '-p', '.hidden', 'x' * 101, 'x;y', 'wt/one', 5, None, ['wt-one']]

_SHAPE_REFUSALS = {
    **{f'stack_up-name-{index}': ('stack_up', {'worktree': name}) for index, name in enumerate(_BAD_NAMES)},
    **{
        f'run_system_tests-name-{index}': ('run_system_tests', {'worktree': name, 'paths': []})
        for index, name in enumerate(_BAD_NAMES)
    },
    **{f'seed_dump-name-{index}': ('seed_dump', {'worktree': name}) for index, name in enumerate(_BAD_NAMES)},
    # THE CLOSED SCHEMA, SWEPT OVER EVERY VERB (tj-tq2hn6 R4). This was two hand-written cases, ps
    # and stack_up, so neither dev verb had ever been shown to refuse an unknown keyword -- a verb
    # was covered only if someone remembered to add it. Generated from VERB_SAMPLES instead, which
    # test_commands.test_every_verb_has_a_sample pins equal to AgentStack's verb table, so the next
    # verb added is covered by construction rather than by memory.
    **{f'unknown keyword on {verb}': (verb, {**sample, 'bogus': 1}) for verb, sample in VERB_SAMPLES.items()},
    'a compose argument smuggled as a keyword': ('stack_down', {'volumes': True}),
    'missing required': ('stack_up', {}),
    'positional (a list)': ('stack_up', [WORKTREE_NAME]),
    'positional (a string)': ('ps', 'ps --all'),
    'service outside the enum': ('logs', {'service': 'redis'}),
    'service with a shell tail': ('logs', {'service': 'postgres; rm -rf /'}),
    'service as another project': ('logs', {'service': 'trader_joe-postgres-1'}),
    'tail a string': ('logs', {'service': 'postgres', 'tail': '10'}),
    'tail a bool': ('logs', {'service': 'postgres', 'tail': True}),
    'paths a string': ('run_system_tests', {'worktree': WORKTREE_NAME, 'paths': 'tests/system'}),
    'paths too many': ('run_system_tests', {'worktree': WORKTREE_NAME, 'paths': ['tests/system'] * 51}),
    'path ../': ('run_system_tests', {'worktree': WORKTREE_NAME, 'paths': ['../x']}),
    'path escaping by ..': ('run_system_tests', {'worktree': WORKTREE_NAME, 'paths': ['tests/system/../../common']}),
    'path absolute': ('run_system_tests', {'worktree': WORKTREE_NAME, 'paths': ['/etc/passwd']}),
    'path an option': ('run_system_tests', {'worktree': WORKTREE_NAME, 'paths': ['-k']}),
    'path a node id option': ('run_system_tests', {'worktree': WORKTREE_NAME, 'paths': ['--pdb']}),
    'path a sibling prefix': ('run_system_tests', {'worktree': WORKTREE_NAME, 'paths': ['tests/systemx']}),
    'path outside tests': ('run_system_tests', {'worktree': WORKTREE_NAME, 'paths': ['common']}),
    'path empty': ('run_system_tests', {'worktree': WORKTREE_NAME, 'paths': ['']}),
    'path a nul': ('run_system_tests', {'worktree': WORKTREE_NAME, 'paths': ['tests/system/\0x']}),
    'path not a string': ('run_system_tests', {'worktree': WORKTREE_NAME, 'paths': [7]}),
}


@pytest.mark.parametrize(('verb', 'arguments'), list(_SHAPE_REFUSALS.values()), ids=list(_SHAPE_REFUSALS))
def test_a_malformed_call_is_refused_with_no_git_no_copy_and_no_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, arguments: Any
):
    """Body bullet 2, (5) as replaced and G3: the shape of every argument is checked before ANY subprocess.

    A worktree NAME is refused with zero git calls and zero docker calls; a bad test path before
    the snapshot refresh, git and docker (D9); an unknown keyword is refused, never dropped.
    """
    rig = make_rig(tmp_path, monkeypatch)
    copies = []
    monkeypatch.setattr(stack, 'refresh_snapshot', lambda *a, **k: copies.append(a))
    result = rig.call(verb, arguments)
    assert result['status'] == 'refused', result
    _no_subprocess(rig)
    assert copies == [], 'the snapshot was refreshed for a refused call'
    assert json.loads(rig.audit_lines()[-1])['arguments'] is None, 'a refused argument reached the audit log'


def test_every_verb_is_swept_for_an_unknown_keyword():
    """tj-tq2hn6 R4: the generated sweep above must cover every verb, not the two it used to name."""
    swept = {verb for name, (verb, _) in _SHAPE_REFUSALS.items() if name.startswith('unknown keyword on ')}
    assert swept == set(VERB_SAMPLES), f'not swept: {sorted(set(VERB_SAMPLES) - swept)}'


@pytest.mark.parametrize('verb', ['stack_up', 'run_system_tests', 'seed_dump'])
def test_a_worktree_git_does_not_list_is_refused_after_one_listing_and_no_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    rig = make_rig(tmp_path, monkeypatch)
    arguments = {**VERB_SAMPLES[verb], 'worktree': 'not-a-worktree'}
    result = rig.call(verb, arguments)
    assert result['status'] == 'refused' and "no worktree named 'not-a-worktree'" in result['message'], result
    _no_subprocess(rig, git_list_calls=1)


def test_a_worktree_outside_the_repository_is_not_listed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """list_worktrees names only worktrees under the repository root (the ones the MCP container sees)."""
    rig = make_rig(tmp_path, monkeypatch)
    elsewhere = rig.layout.root / 'elsewhere' / 'wt-far'
    shutil.copytree(rig.layout.worktree, elsewhere)
    rig.git.worktree_paths.append(elsewhere)
    result = rig.call('stack_up', {'worktree': 'wt-far'})
    assert result['status'] == 'refused', result
    assert rig.docker.steps == []


def test_a_test_path_symlinked_out_of_tests_system_is_refused_before_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Body bullet 2: a symlink out of tests/system is refused -- by the snapshot, which copies no link."""
    rig = make_rig(tmp_path, monkeypatch)
    (rig.layout.worktree / 'tests' / 'system' / 'out').symlink_to(rig.layout.repo / 'common', target_is_directory=True)
    result = rig.call('run_system_tests', {'worktree': WORKTREE_NAME, 'paths': ['tests/system/out']})
    assert result['status'] == 'refused' and 'symlink' in result['message'], result
    assert rig.docker.steps == []


def test_a_test_path_missing_from_the_snapshot_is_refused_before_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = make_rig(tmp_path, monkeypatch)
    result = rig.call('run_system_tests', {'worktree': WORKTREE_NAME, 'paths': ['tests/system/missing.py']})
    assert result['status'] == 'refused' and 'does not resolve' in result['message'], result
    assert rig.docker.steps == []


def test_valid_test_paths_are_normalised_and_an_empty_list_runs_the_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = make_rig(tmp_path, monkeypatch)
    rig.call(
        'run_system_tests',
        {'worktree': WORKTREE_NAME, 'paths': ['tests/system/./sub/../test_one.py', 'tests/system/sub']},
    )
    rig.call('run_system_tests', {'worktree': WORKTREE_NAME, 'paths': []})
    # Each call first inspects every BASE_IMAGES ref (ADR tj-4rr0la addendum 14 (3); re-pinned,
    # tj-c4mosr.14). The path normalisation below is unchanged.
    inspects = [('/usr/local/bin/docker', 'image', 'inspect', '--format', '{{.Id}}', ref) for ref in stack.BASE_IMAGES]
    assert [step.argv for step in rig.docker.steps if step.argv[1] != 'compose'] == inspects * 2
    steps = rig.docker.steps
    assert [step.argv for step in steps[: len(inspects)]] == inspects
    assert [step.argv for step in steps[len(inspects) + 1 : 2 * len(inspects) + 1]] == inspects
    tails = [list(step.argv[step_prefix_length() :]) for step in steps if step.argv[1] == 'compose']
    assert tails == [
        ['run', '--rm', '--no-deps', '--build', 'test_client', 'tests/system/test_one.py', 'tests/system/sub'],
        ['run', '--rm', '--no-deps', '--build', 'test_client', 'tests/system'],
    ]


def test_migrate_needs_a_recorded_stack_up_and_a_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """D4: migrate uses the worktree the last stack_up recorded, refused when none is; no docker either way."""
    rig = make_rig(tmp_path, monkeypatch)
    for verb in ('migrate', 'migrate_status'):
        result = rig.call(verb, {})
        assert result['status'] == 'refused' and 'call stack_up first' in result['message'], result
    record_state(rig.layout, 'gone-worktree')
    assert rig.call('migrate', {})['status'] == 'refused'
    record_state(rig.layout)
    for path in (rig.layout.worktree / 'data/store/migrations/versions').iterdir():
        path.unlink()
    result = rig.call('migrate', {})
    assert result['status'] == 'refused' and 'no revision files' in result['message'], result
    assert rig.docker.steps == []


def test_migrate_fails_when_postgres_is_not_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = make_rig(tmp_path, monkeypatch, FakeDocker(respond=lambda step: runner.ProcessResult(0, b'', b'')))
    record_state(rig.layout)
    result = rig.call('migrate', {})
    assert result['status'] == 'failed' and 'postgres is not running' in result['message']
    assert [step.argv[step_prefix_length() :] for step in rig.docker.steps] == [('ps', '-q', 'postgres')]


# --- one verb at a time ------------------------------------------------------------------------


def test_a_second_verb_while_one_runs_answers_busy_and_the_lock_is_released_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Body bullet 6: busy, naming the running verb -- never a queue."""
    release = asyncio.Event()
    entered = asyncio.Event()

    async def hold(step: stack.Step) -> None:
        if 'build' in step.argv:
            entered.set()
            await release.wait()

    rig = make_rig(tmp_path, monkeypatch, FakeDocker(hook=hold))

    async def scenario() -> tuple[dict, dict, dict]:
        first = asyncio.create_task(rig.agent.call('stack_up', {'worktree': WORKTREE_NAME}))
        await asyncio.wait_for(entered.wait(), 10)
        busy = await rig.agent.call('ps', {})
        release.set()
        up = await first
        after = await rig.agent.call('ps', {})
        return up, busy, after

    up, busy, after = asyncio.run(scenario())
    assert busy['status'] == 'busy' and 'stack_up is running' in busy['message'], busy
    assert busy['steps'] == []
    assert up['status'] == 'ok' and after['status'] == 'ok'
    assert [json.loads(line)['status'] for line in rig.audit_lines()] == ['busy', 'ok', 'ok']


def test_a_slow_git_answers_busy_promptly_and_the_timeout_fires_during_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """L: git runs off the loop, so a blocked git thread neither delays 'busy' nor the verb's timeout."""
    rig = make_rig(tmp_path, monkeypatch)
    rig.git.delay = 1.5
    monkeypatch.setitem(runner.VERB_TIMEOUT_SECONDS, 'stack_up', 0.5)

    async def scenario() -> tuple[dict, float, dict, float]:
        start = time.monotonic()
        first = asyncio.create_task(rig.agent.call('stack_up', {'worktree': WORKTREE_NAME}))
        await asyncio.sleep(0.05)
        busy = await rig.agent.call('ps', {})
        busy_at = time.monotonic() - start
        up = await first
        return busy, busy_at, up, time.monotonic() - start

    busy, busy_at, up, up_at = asyncio.run(scenario())
    assert busy['status'] == 'busy' and busy_at < 0.4, f'busy answered at {busy_at:.2f}s'
    assert up['status'] == 'timeout' and up_at < 1.2, f'the timeout fired at {up_at:.2f}s, not while git ran'
    assert rig.docker.steps == []


def test_a_timed_out_copy_stops_and_the_old_snapshot_survives_until_the_next_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """G4 / D8: the verb's timeout sets the copy's cancel event; the copy thread stops without swapping."""
    rig = make_rig(tmp_path, monkeypatch)
    for index in range(60):
        (rig.layout.worktree / 'common' / f'm{index}.py').write_text('')
    assert rig.call('stack_up', {'worktree': WORKTREE_NAME})['status'] == 'ok'
    before = tree_digest(rig.layout.snapshot)
    (rig.layout.worktree / 'common' / 'changed.py').write_text('NEW = 1\n')

    real_tick = stack._Copy.tick
    monkeypatch.setattr(stack._Copy, 'tick', lambda self, relative: (time.sleep(0.02), real_tick(self, relative))[1])
    monkeypatch.setitem(runner.VERB_TIMEOUT_SECONDS, 'stack_up', 0.3)
    rig.docker.steps.clear()
    timed_out = rig.call('stack_up', {'worktree': WORKTREE_NAME})
    assert timed_out['status'] == 'timeout', timed_out
    assert rig.docker.steps == []
    assert stack._SNAPSHOT_LOCK.acquire(timeout=10), 'the copy thread never stopped'
    stack._SNAPSHOT_LOCK.release()
    assert tree_digest(rig.layout.snapshot) == before, 'the cancelled copy was swapped in anyway'
    assert not os.path.lexists(rig.layout.stack_dir / 'source.new')

    monkeypatch.setattr(stack._Copy, 'tick', real_tick)
    monkeypatch.setitem(runner.VERB_TIMEOUT_SECONDS, 'stack_up', 1800)
    assert rig.call('stack_up', {'worktree': WORKTREE_NAME})['status'] == 'ok'
    assert (rig.layout.snapshot / 'common' / 'changed.py').exists()


# --- timeouts, errors, output ----------------------------------------------------------------


def test_a_hung_step_times_out_writes_one_audit_line_and_releases_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """(8): status 'timeout', one audit line, and the next verb runs."""
    hang = {'on': True}

    async def maybe_hang(step: stack.Step) -> None:
        if hang['on']:
            await asyncio.sleep(3600)

    rig = make_rig(tmp_path, monkeypatch, FakeDocker(hook=maybe_hang))
    monkeypatch.setitem(runner.VERB_TIMEOUT_SECONDS, 'ps', 0.2)
    result = rig.call('ps', {})
    assert result['status'] == 'timeout' and 'timeout' in result['message'], result
    assert [json.loads(line)['status'] for line in rig.audit_lines()] == ['timeout']
    hang['on'] = False
    assert rig.call('ps', {})['status'] == 'ok'


def test_an_unexpected_exception_is_status_error_and_still_audited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def explode(step: stack.Step) -> runner.ProcessResult:
        raise RuntimeError('daemon went away')

    rig = make_rig(tmp_path, monkeypatch, FakeDocker(respond=explode))
    result = rig.call('ps', {})
    assert result['status'] == 'error' and 'RuntimeError' in result['message']
    assert json.loads(rig.audit_lines()[-1])['status'] == 'error'
    assert rig.call('ps', {})['status'] == 'error', 'the lock was not released after an error'


def test_a_failing_step_stops_the_verb_as_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def fail_build(step: stack.Step) -> runner.ProcessResult:
        return runner.ProcessResult(1 if 'build' in step.argv else 0, b'', b'boom')

    rig = make_rig(tmp_path, monkeypatch, FakeDocker(respond=fail_build))
    result = rig.call('stack_up', {'worktree': WORKTREE_NAME})
    assert result['status'] == 'failed' and result['exit_status'] == 1
    # The base inspects run (and succeed) before the build (re-pinned, tj-c4mosr.14). The property:
    # nothing runs after the failed step -- the failed build is LAST and `up` never ran.
    exits = [step['exit_status'] for step in result['steps']]
    assert exits == [0] * len(stack.BASE_IMAGES) + [1], result['steps']
    commands = [step['command'].split() for step in result['steps']]
    assert 'build' in commands[-1], f'the last step is not the failed build: {commands[-1]}'
    assert not any('up' in command for command in commands), 'a step ran after a failed one: up'
    assert [step.argv for step in rig.docker.steps if 'up' in step.argv] == []


def test_output_keeps_its_tail_and_says_it_was_cut(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """(8): past OUTPUT_CAP_BYTES the TAIL is kept -- a failure explains itself at the end."""
    cap = runner.OUTPUT_CAP_BYTES
    long = b'HEAD' + b'.' * (cap + 100) + b'THE_END'
    rig = make_rig(tmp_path, monkeypatch, FakeDocker(respond=lambda step: runner.ProcessResult(0, long, b'short')))
    result = rig.call('ps', {})
    stdout, stderr = result['steps'][0]['stdout'], result['steps'][0]['stderr']
    assert stdout['truncated'] is True and stdout['text'].endswith('THE_END') and 'HEAD' not in stdout['text']
    assert len(stdout['text'].encode()) == cap and f'the last {cap} of {len(long)} bytes' in stdout['note']
    assert stderr == {'text': 'short', 'truncated': False}
    assert runner.truncate(b'x' * cap) == {'text': 'x' * cap, 'truncated': False}
    assert result['output_cap_bytes'] == cap == 16_000


# --- the audit log ---------------------------------------------------------------------------


def test_every_call_writes_exactly_one_audit_line_with_no_secret_in_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """(9): unknown verb, unknown keyword, busy, refusal, timeout, error, ok -- one line each, no values leak."""
    mode = {'value': 'ok'}

    async def behave(step: stack.Step) -> None:
        if mode['value'] == 'hang':
            await asyncio.sleep(3600)
        if mode['value'] == 'error':
            raise RuntimeError('boom')

    rig = make_rig(
        tmp_path,
        monkeypatch,
        FakeDocker(hook=behave, respond=lambda step: runner.ProcessResult(0, LIVE_ENV_SENTINEL.encode(), b'')),
    )
    monkeypatch.setitem(runner.VERB_TIMEOUT_SECONDS, 'ps', 0.2)
    statuses = []
    statuses.append(rig.call('no_such_verb', {'x': LIVE_ENV_SENTINEL})['status'])
    statuses.append(rig.call('ps', {'secret': LIVE_ENV_SENTINEL})['status'])
    statuses.append(rig.call('stack_up', {'worktree': f'../{LIVE_ENV_SENTINEL}'})['status'])
    statuses.append(rig.call('logs', {'service': 'postgres'})['status'])
    mode['value'] = 'hang'
    statuses.append(rig.call('ps', {})['status'])
    mode['value'] = 'error'
    statuses.append(rig.call('ps', {})['status'])
    mode['value'] = 'ok'
    rig.agent._running = 'stack_up'
    statuses.append(rig.call('ps', {})['status'])
    rig.agent._running = None
    lines = [json.loads(line) for line in rig.audit_lines()]
    assert statuses == ['refused', 'refused', 'refused', 'ok', 'timeout', 'error', 'busy']
    assert [line['status'] for line in lines] == statuses
    assert [line['known'] for line in lines] == [False, True, True, True, True, True, True]
    assert lines[3]['arguments'] == {'service': 'postgres', 'tail': 200}
    assert all(line['arguments'] is None for index, line in enumerate(lines) if statuses[index] == 'refused')
    assert set(lines[0]) == {'time', 'verb', 'known', 'arguments', 'status', 'exit_status', 'duration_s'}
    text = (rig.layout.stack_dir / stack.AUDIT_LOG_NAME).read_text()
    root_env = stack.parse_env_text((rig.layout.stack_dir / 'agent_stack.env').read_text())
    for secret in (LIVE_ENV_SENTINEL, root_env['POSTGRES_PASS'], root_env['INSTANCE_WRITE_SECRET']):
        assert secret not in text, 'a secret, an argument value or command output reached the audit log'
    assert os.stat(rig.layout.stack_dir / stack.AUDIT_LOG_NAME).st_mode & 0o777 == 0o600


def test_an_unknown_verb_is_audited_by_its_name_escaped_and_cut_to_64(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A: the attempted name, JSON-escaped, truncated to 64 characters, known: false."""
    rig = make_rig(tmp_path, monkeypatch)
    name = 'evil"\nverb\t' + 'x' * 100
    result = rig.call(name, {})
    assert result['status'] == 'refused' and result['verb'] is None
    lines = rig.audit_lines()
    assert len(lines) == 1, 'the name broke the one-line-per-call format'
    line = json.loads(lines[0])
    assert line['verb'] == name[: runner.AUDIT_VERB_NAME_MAX] and runner.AUDIT_VERB_NAME_MAX == 64
    assert line['known'] is False and line['arguments'] is None


def test_a_planted_audit_symlink_is_not_written_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rig = make_rig(tmp_path, monkeypatch)
    target = rig.layout.root / 'elsewhere.log'
    target.write_text('')
    (rig.layout.stack_dir / stack.AUDIT_LOG_NAME).symlink_to(target)
    result = rig.call('ps', {})
    assert 'audit log could not be written' in result['audit']
    assert target.read_text() == ''


# --- the snapshot through the verbs ------------------------------------------------------------


def test_a_worktree_swapped_for_a_symlink_mid_verb_never_reaches_the_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """S5, the regression for the RE on tj-c4mosr.3 item 1, and its companion (02:14 (3)).

    During the build step the worktree's data/store/migrations becomes a symlink out of it. The later
    up step still names only the snapshot, whose migrations is a real directory with the original
    content; the next migrate refuses the symlinked worktree with zero docker calls.
    """
    layout_holder: dict[str, Any] = {}
    observed: dict[str, Any] = {}

    def swap_and_observe(step: stack.Step) -> None:
        layout = layout_holder['layout']
        migrations = layout.worktree / 'data' / 'store' / 'migrations'
        if 'build' in step.argv:
            outside = layout.root / 'attacker_migrations'
            outside.mkdir()
            (outside / 'evil.py').write_text('EVIL = 1\n')
            shutil.rmtree(migrations)
            migrations.symlink_to(outside, target_is_directory=True)
        if 'up' in step.argv:
            snapshot_migrations = layout.snapshot / 'data' / 'store' / 'migrations'
            observed['is_link'] = snapshot_migrations.is_symlink()
            observed['files'] = sorted(str(p.relative_to(snapshot_migrations)) for p in snapshot_migrations.rglob('*'))
            observed['argv'] = step.argv

    rig = make_rig(tmp_path, monkeypatch, FakeDocker(hook=swap_and_observe))
    layout_holder['layout'] = rig.layout
    result = rig.call('stack_up', {'worktree': WORKTREE_NAME})
    assert result['status'] == 'ok', result
    assert observed, 'the up step never ran'
    assert observed['is_link'] is False and observed['files'] == ['env.py', 'versions', 'versions/0001_initial.py']
    assert not [word for word in observed['argv'] if str(rig.layout.worktree) in word or 'attacker' in word]

    rig.docker.steps.clear()
    migrate = rig.call('migrate', {})
    assert migrate['status'] == 'refused' and 'symlink' in migrate['message'], migrate
    assert rig.docker.steps == []


# The dev pair belongs here too (tj-v4e9ke, from tj-tq2hn6's residual): _dev_steps's docstring claims
# 'the snapshot is not used -- these verbs read no worktree at all', and until they were listed that
# claim was unpinned. migrate_check, added after this list was written, does NOT belong: it BUILDS
# data_store before it compares (stack.alembic_check_steps), so it is a BUILDING_VERB in test_bases
# and reads the snapshot like every other alembic verb. Everything here is in BASE_FREE_VERBS.
@pytest.mark.parametrize('verb', ['stack_down', 'stack_wipe', 'logs', 'ps', 'dev_ps', 'dev_logs'])
def test_verbs_that_build_nothing_read_no_worktree_and_need_no_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
):
    """S6: never a copy from any worktree; an empty snapshot is made when there is none.

    The dev pair is held to the STRONGER half of that, and it is the half _dev_steps claims in so
    many words: the snapshot is not used at all. An agent-stack verb still needs the directory to
    exist because compose is handed it as --project-directory; a dev verb names no project
    directory, so the snapshot must still be absent after it has run.
    """
    rig = make_rig(tmp_path, monkeypatch)

    def no_copy(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(f'{verb} copied a worktree')

    monkeypatch.setattr(stack, 'refresh_snapshot', no_copy)
    monkeypatch.setattr(stack, '_copy_source', no_copy)
    assert not rig.layout.snapshot.exists()
    result = rig.call(verb, dict(VERB_SAMPLES[verb]))
    assert result['status'] == 'ok', result
    if verb in DEV_PROJECT_VERBS:
        assert not rig.layout.snapshot.exists(), 'a dev verb reached the snapshot'
    else:
        assert rig.layout.snapshot.is_dir() and list(rig.layout.snapshot.iterdir()) == []
    assert all(call == ('worktree', 'list', '--porcelain') or call[0] == 'cat-file' for call in rig.git.calls)


# --- the subprocess itself: a fixed environment, never the container's --------------------------


def test_run_process_hands_the_child_exactly_docker_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """(7): DOCKER_ENV is the fixed four keys, DOCKER_HOST the proxy, and run_process passes it -- not os.environ."""
    assert runner.DOCKER_ENV == {
        'PATH': '/usr/local/bin:/usr/bin:/bin',
        'HOME': '/home/agent_mcp',
        'DOCKER_HOST': 'tcp://socket_proxy:2375',
        'LC_ALL': 'C.UTF-8',
    }
    for name, value in {'COMPOSE_FILE': 'x', 'DATA_DIR': '/elsewhere', 'AGENT_HOME_PATH': '/agent_mcp_share'}.items():
        monkeypatch.setenv(name, value)
    env_binary = shutil.which('env')
    assert env_binary, 'env is not on PATH'
    result = asyncio.run(runner.run_process(stack.Step((env_binary,), tmp_path)))
    assert result.exit_status == 0
    printed = dict(line.split('=', 1) for line in result.stdout.decode().splitlines())
    assert printed == runner.DOCKER_ENV
    cwd = asyncio.run(runner.run_process(stack.Step((shutil.which('pwd') or '/bin/pwd',), tmp_path)))
    assert cwd.stdout.decode().strip() == os.path.realpath(tmp_path)
    cat = asyncio.run(asyncio.wait_for(runner.run_process(stack.Step((shutil.which('cat'),), tmp_path)), 5))
    assert cat.stdout == b'', 'stdin was not /dev/null'


def test_run_process_kills_its_child_when_the_verb_is_cancelled(tmp_path: Path):
    marker = tmp_path / 'still_running'
    script = f'sleep 0.6; touch {marker}'

    async def scenario() -> None:
        task = asyncio.create_task(runner.run_process(stack.Step((shutil.which('sh'), '-c', script), tmp_path)))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    time.sleep(1.0)
    assert not marker.exists(), 'the child outlived the cancelled verb'
