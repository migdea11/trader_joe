"""AgentStack: validation before any subprocess, one verb at a time, timeouts, output, the audit log.

tj-c4mosr.5 body bullets 2 and 6, items (5) as replaced, G3, G4, L, (7), (8), (9), A, S5 (and its
companion), S6. Design: ADR tj-4rr0la section 3, 5(a), addenda 5 and 6 (D4, D8, D9, D10).

Timings are measured INSIDE one running event loop (the validator's 02:14 note): asyncio.run() waits
for the default executor at shutdown, so a wrapper around it would report a blocked git thread's
whole sleep instead of when the verb answered.
"""

import asyncio
import contextlib
import json
import os
import secrets
import shutil
import signal
import subprocess  # nosec B404 -- a sleeping sibling in this process's group, by argument list
import time
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tools.agent_mcp import runner, stack
from tools.agent_mcp.tests.harness import (
    DEV_PROJECT_VERBS,
    LIVE_ENV_SENTINEL,
    REPO_ROOT,
    VERB_SAMPLES,
    WORKTREE_NAME,
    FakeDocker,
    budget_spent,
    client_tail,
    is_base_step,
    is_client_step,
    make_rig,
    system_suite_response,
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


def _suite_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A rig whose project is in TEST mode on the test postgres mount, so run_system_tests passes its rails (tj-iee6sn.20)."""
    rig = make_rig(tmp_path, monkeypatch)
    rig.docker.respond = system_suite_response(rig.layout)
    return rig


def test_a_test_path_symlinked_out_of_tests_system_is_refused_before_the_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Body bullet 2: a symlink out of tests/system is refused -- by the snapshot, which copies no link.

    Re-pinned by tj-iee6sn.20: the rails' docker reads now run before the snapshot, so what is pinned is
    that no client step (and no base step) ran, and that the budget was not charged.
    """
    rig = _suite_rig(tmp_path, monkeypatch)
    (rig.layout.worktree / 'tests' / 'system' / 'out').symlink_to(rig.layout.repo / 'common', target_is_directory=True)
    result = rig.call('run_system_tests', {'worktree': WORKTREE_NAME, 'paths': ['tests/system/out']})
    assert result['status'] == 'refused' and 'symlink' in result['message'], result
    assert rig.docker.steps, 'the rails ran no docker read, so the refusal shows nothing about their order'
    assert not [step for step in rig.docker.steps if is_client_step(step) or is_base_step(step)]
    assert budget_spent(rig) == 0


def test_a_test_path_missing_from_the_snapshot_is_refused_before_the_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = _suite_rig(tmp_path, monkeypatch)
    result = rig.call('run_system_tests', {'worktree': WORKTREE_NAME, 'paths': ['tests/system/missing.py']})
    assert result['status'] == 'refused' and 'does not resolve' in result['message'], result
    assert not [step for step in rig.docker.steps if is_client_step(step) or is_base_step(step)]
    assert budget_spent(rig) == 0


def test_valid_test_paths_are_normalised_and_an_empty_list_runs_the_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rig = _suite_rig(tmp_path, monkeypatch)
    assert (
        rig.call(
            'run_system_tests',
            {'worktree': WORKTREE_NAME, 'paths': ['tests/system/./sub/../test_one.py', 'tests/system/sub']},
        )['status']
        == 'ok'
    )
    assert rig.call('run_system_tests', {'worktree': WORKTREE_NAME, 'paths': []})['status'] == 'ok'
    # Each call inspects every BASE_IMAGES ref (ADR tj-4rr0la addendum 14 (3)) right before its one client
    # step. The path normalisation below is unchanged (re-pinned to the client prefix by tj-iee6sn.20).
    inspects = [('/usr/local/bin/docker', 'image', 'inspect', '--format', '{{.Id}}', ref) for ref in stack.BASE_IMAGES]
    assert [step.argv for step in rig.docker.steps if is_base_step(step)] == inspects * 2
    clients = [index for index, step in enumerate(rig.docker.steps) if is_client_step(step)]
    assert len(clients) == 2
    for index in clients:
        assert [step.argv for step in rig.docker.steps[index - len(inspects) : index]] == inspects
    tails = [client_tail(rig.docker.steps[index], rig.layout) for index in clients]
    assert tails == [
        ['run', '--rm', '--no-deps', '--build', 'test_client', 'tests/system/test_one.py', 'tests/system/sub'],
        ['run', '--rm', '--no-deps', '--build', 'test_client', 'tests/system'],
    ]


def test_migrate_reads_the_root_checkouts_revisions_and_no_worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """tj-iee6sn.10: migrate acts on the mode that is up, with the ROOT checkout's revisions (make migrate's).

    The agent-stack migrate (the recorded stack_up's worktree, its snapshot, `ps -q postgres`) is gone: no
    recorded worktree is needed, emptying the agent WORKTREE's versions/ changes nothing, and emptying the
    ROOT checkout's refuses with the empty-versions guard and no docker step after the mode guard.
    """
    rig = make_rig(tmp_path, monkeypatch)
    assert not (rig.layout.stack_dir / stack.STATE_FILE_NAME).exists()
    for path in (rig.layout.worktree / 'server/data/store/migrations/versions').iterdir():
        path.unlink()
    assert rig.call('migrate', {})['status'] == 'ok'
    assert not rig.layout.snapshot.exists(), 'migrate took a snapshot'
    for path in (rig.layout.repo / 'server/data/store/migrations/versions').iterdir():
        path.unlink()
    rig.docker.steps.clear()
    result = rig.call('migrate', {})
    assert result['status'] == 'refused' and 'no revision files' in result['message'], result
    assert [step.argv[:3] for step in rig.docker.steps] == [('/usr/local/bin/docker', 'ps', '-a')], 'the guard only'


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
        (rig.layout.worktree / 'server' / 'common' / f'm{index}.py').write_text('')
    assert rig.call('stack_up', {'worktree': WORKTREE_NAME})['status'] == 'ok'
    before = tree_digest(rig.layout.snapshot)
    (rig.layout.worktree / 'server' / 'common' / 'changed.py').write_text('NEW = 1\n')

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
    assert (rig.layout.snapshot / 'server' / 'common' / 'changed.py').exists()


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
    content; the next stack_up refuses the symlinked worktree with zero docker calls. (Its companion was the
    agent-stack migrate, removed by tj-iee6sn.10; stack_up snapshots the same worktree the same way.)
    """
    layout_holder: dict[str, Any] = {}
    observed: dict[str, Any] = {}

    def swap_and_observe(step: stack.Step) -> None:
        layout = layout_holder['layout']
        migrations = layout.worktree / 'server' / 'data' / 'store' / 'migrations'
        if 'build' in step.argv:
            outside = layout.root / 'attacker_migrations'
            outside.mkdir()
            (outside / 'evil.py').write_text('EVIL = 1\n')
            shutil.rmtree(migrations)
            migrations.symlink_to(outside, target_is_directory=True)
        if 'up' in step.argv:
            snapshot_migrations = layout.snapshot / 'server' / 'data' / 'store' / 'migrations'
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
    again = rig.call('stack_up', {'worktree': WORKTREE_NAME})
    assert again['status'] == 'refused' and 'symlink' in again['message'], again
    assert rig.docker.steps == []


# The dev pair belongs here too (tj-v4e9ke, from tj-tq2hn6's residual): _dev_steps's docstring claims
# 'the snapshot is not used -- these verbs read no worktree at all', and until they were listed that
# claim was unpinned. Everything here is in BASE_FREE_VERBS, migrate_status included (tj-iee6sn.10: the
# mode guard, a lookup and an exec into the running data_store; the agent-stack alembic verbs are gone).
@pytest.mark.parametrize(
    'verb',
    ['stack_down', 'stack_wipe', 'logs', 'ps', 'dev_ps', 'dev_logs', 'dev_db_tables', 'migrate_status', 'dev_http_get'],
)
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


# --- a cancelled step's WHOLE process group dies (tj-iee6sn.15, B5b) -------------------------------
#
# Design checked against: tj-iee6sn.15's WHAT (2) and TESTS (iv), from ruling (1)(b) of the architect's
# 16:59 UTC gate note on tj-iee6sn.8: the runner promises nothing a step started outlives the verb, so
# every step gets its own session, and on cancellation the group gets SIGTERM, then -- always --
# SIGKILL after the grace, and the leader is reaped. Liveness is read from /proc/<pid>/status: a killed
# orphan stays a zombie (state Z) where nothing reaps it, and os.kill(pid, 0) succeeds on a zombie.
# Every process is found by a MARKER in its command line, a sleep duration no other process uses, and
# the fixture SIGKILLs whatever is still alive afterwards, pass or fail (tj-smsty8).

_BASH = shutil.which('bash') or '/bin/bash'
# A scenario that hangs (a mutation that never sends SIGKILL, or kills no group) fails here, not forever.
_SCENARIO_TIMEOUT = 15.0


def _cmdline(pid: int) -> bytes:
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as handle:
            return handle.read()
    except OSError:
        return b''


def _proc_field(pid: int, name: str) -> str | None:
    try:
        with open(f'/proc/{pid}/status', encoding='utf-8') as handle:
            for line in handle:
                if line.startswith(f'{name}:'):
                    return line.split()[1]
    except OSError:
        return None
    return None


def _marked(marker: str) -> list[int]:
    """Every pid but this one whose command line carries MARKER, zombies included."""
    token = marker.encode()
    pids = (int(name) for name in os.listdir('/proc') if name.isdigit())
    return [pid for pid in pids if pid != os.getpid() and token in _cmdline(pid)]


def _alive(marker: str) -> list[int]:
    """The marked pids that are still running: gone, zombie (Z) and dead (X) all count as dead."""
    return [pid for pid in _marked(marker) if _proc_field(pid, 'State') not in (None, 'Z', 'X')]


def _kill_marked(marker: str) -> None:
    for pid in _alive(marker):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


@pytest.fixture
def marker() -> Iterator[str]:
    """A sleep duration of 600 s and change, unique to this test; whatever carries it is killed after."""
    token = f'{600 + secrets.randbelow(100)}.{secrets.randbelow(10**9):09d}'
    try:
        yield token
    finally:
        _kill_marked(token)


async def _until(predicate: Callable[[], bool], seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('the condition never held')
        await asyncio.sleep(0.02)


def _children_of_this_process(marker: str) -> list[int]:
    return [pid for pid in _marked(marker) if _proc_field(pid, 'PPid') == str(os.getpid())]


async def _start_then_cancel(
    step: stack.Step, marker: str, ready: Callable[[], bool], spare: int | None = None
) -> tuple[int, float]:
    """Start STEP, wait for READY, cancel it, and return (the leader's pid, seconds from cancel to done)."""
    task = asyncio.create_task(runner.run_process(step))
    await _until(ready)
    (leader,) = [pid for pid in _children_of_this_process(marker) if pid != spare]
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return leader, time.monotonic() - started


def _run_scenario(scenario: Awaitable[Any]) -> Any:
    async def bounded() -> Any:
        return await asyncio.wait_for(scenario, _SCENARIO_TIMEOUT)

    return asyncio.run(bounded())


def test_a_cancelled_step_leaves_no_descendant_alive_even_one_ignoring_sigterm(tmp_path: Path, marker: str):
    """A shell and two children all ignoring SIGTERM: the SIGKILL after the grace takes every one."""
    script = f'trap "" TERM; sleep {marker} & sleep {marker} & wait'
    step = stack.Step((_BASH, '-c', script), tmp_path)
    leader, took = _run_scenario(_start_then_cancel(step, marker, lambda: len(_alive(marker)) == 3))
    assert _alive(marker) == [], f'outlived the cancelled verb: {_alive(marker)}'
    assert not os.path.exists(f'/proc/{leader}'), 'the leader was not reaped'
    assert took >= runner._KILL_GRACE_SECONDS * 0.9, f'SIGKILL came before the grace: {took:.2f}s'


def test_a_child_left_behind_by_an_exited_leader_is_killed_too(tmp_path: Path, marker: str):
    """The leader is gone, its child holds stdout open: killing the leader's pid alone would kill nothing."""
    step = stack.Step((_BASH, '-c', f'trap "" TERM; sleep {marker} & exit 0'), tmp_path)

    async def scenario() -> None:
        task = asyncio.create_task(runner.run_process(step))
        await _until(lambda: len(_alive(marker)) == 1 and not _children_of_this_process(marker), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The leader was reaped long ago, so run_process returns before the loop has seen the killed
        # child's pipes close; let it, or the transport is collected after asyncio.run closed the loop.
        await asyncio.sleep(0.3)

    # The leader exits at once and is reaped by the child watcher; the sleep, re-parented, is what is left.
    _run_scenario(scenario())
    assert _alive(marker) == [], f'outlived the cancelled verb: {_alive(marker)}'


def test_the_group_gets_sigterm_then_always_sigkill_by_its_own_id(
    tmp_path: Path, marker: str, monkeypatch: pytest.MonkeyPatch
):
    """SIGTERM, then SIGKILL to the group even after the leader exited on SIGTERM inside the grace.

    A member that ignored SIGTERM would otherwise survive. Both signals name the step's own group, never ours.
    """
    sent: list[tuple[int, int]] = []
    real_killpg = os.killpg

    def recording_killpg(pgid: int, sig: int) -> None:
        sent.append((pgid, sig))
        real_killpg(pgid, sig)

    monkeypatch.setattr(os, 'killpg', recording_killpg)
    step = stack.Step((_BASH, '-c', f'sleep {marker}; :'), tmp_path)
    leader, took = _run_scenario(_start_then_cancel(step, marker, lambda: len(_alive(marker)) == 2))
    assert sent == [(leader, signal.SIGTERM), (leader, signal.SIGKILL)], sent
    assert leader != os.getpgrp()
    assert took < runner._KILL_GRACE_SECONDS, f'a group that exits on SIGTERM waited out the grace: {took:.2f}s'
    assert _alive(marker) == []


def test_each_step_is_its_own_session_and_the_kill_spares_this_process_group(tmp_path: Path, marker: str):
    """A sibling in the MCP's own process group survives the cancel; the step's group is not ours."""
    sibling = subprocess.Popen(['sleep', marker])  # nosec B603 B607 -- in this process's group, on purpose
    try:
        groups: dict[str, int] = {}
        step = stack.Step((_BASH, '-c', f'trap "" TERM; sleep {marker} & wait'), tmp_path)

        def ready() -> bool:
            children = [pid for pid in _children_of_this_process(marker) if pid != sibling.pid]
            if len(_alive(marker)) != 3 or len(children) != 1:
                return False
            groups['step'] = os.getpgid(children[0])
            groups['session'] = os.getsid(children[0])
            groups['leader'] = children[0]
            return True

        _run_scenario(_start_then_cancel(step, marker, ready, spare=sibling.pid))
        assert groups['step'] == groups['session'] == groups['leader'], groups
        assert groups['step'] != os.getpgrp() and os.getpgid(sibling.pid) == os.getpgrp()
        assert sibling.poll() is None and _proc_field(sibling.pid, 'State') not in (None, 'Z', 'X')
        assert _alive(marker) == [sibling.pid], _alive(marker)
    finally:
        sibling.kill()
        sibling.wait(timeout=10)


def test_a_digest_blocked_on_a_fifo_swapped_in_leaves_nothing_behind(tmp_path: Path, marker: str):
    """The case that made this bead: the REAL script, a COPY source swapped for a FIFO after the walk.

    The read of the FIFO blocks in the command substitution's subshell, a child of the script's bash;
    killing bash alone left it blocked after the verb returned. TMPDIR is the step's own, so the test
    can see the walk finish (the sorted `files` list exists) before it swaps the regular file for the
    FIFO; the many files ahead of it in the manifest keep the window wide.
    """
    trusted = tmp_path / f'trusted-{marker}'
    (trusted / 'tools').mkdir(parents=True)
    shutil.copy2(REPO_ROOT / 'tools' / 'source_digest.sh', trusted / 'tools' / 'source_digest.sh')
    (trusted / 'Dockerfile').write_text('FROM scratch\nCOPY ./a_many /a\nCOPY ./z_target /z\n')
    checkout = tmp_path / 'checkout'
    (checkout / 'a_many').mkdir(parents=True)
    for index in range(600):
        (checkout / 'a_many' / f'f{index:04d}.py').write_text(f'{index}\n')
    target = checkout / 'z_target'
    target.write_text('regular\n')
    (checkout / '.dockerignore').write_text('')
    work = tmp_path / 'tmp'
    work.mkdir()
    script = str(trusted / 'tools' / 'source_digest.sh')
    step = stack.Step((_BASH, script, '--context', str(checkout), 'x', 'y'), tmp_path, env=(('TMPDIR', str(work)),))

    async def scenario() -> None:
        task = asyncio.create_task(runner.run_process(step))
        await _until(lambda: any(work.glob('tmp.*/files')), 20)
        target.unlink()
        os.mkfifo(target)
        done, _ = await asyncio.wait({task}, timeout=4)
        assert not done, f'the digest did not block on the FIFO: {task.result() if done else None}'
        assert len(_alive(marker)) >= 2, 'expected the script and its blocked subshell'
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _run_scenario(scenario())
    assert _alive(marker) == [], f'outlived the cancelled verb: {_alive(marker)}'


def test_a_step_that_finishes_returns_its_status_streams_and_cap_and_is_sent_no_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The normal path is unchanged: exit status, both streams, the stdout cap, and no killpg at all."""
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, 'killpg', lambda pgid, sig: sent.append((pgid, sig)))
    plain = stack.Step((_BASH, '-c', 'printf out; printf err >&2; exit 3'), tmp_path)
    capped = stack.Step((_BASH, '-c', 'printf 0123456789; printf err >&2'), tmp_path, stdout_cap=4)

    async def scenario() -> tuple[runner.ProcessResult, runner.ProcessResult, float]:
        started = time.monotonic()
        first = await runner.run_process(plain)
        second = await runner.run_process(capped)
        return first, second, time.monotonic() - started

    first, second, took = _run_scenario(scenario())
    assert first == runner.ProcessResult(3, b'out', b'err')
    assert second == runner.ProcessResult(0, b'01234', b'err')
    assert sent == [] and took < runner._KILL_GRACE_SECONDS


def test_a_child_that_exits_before_the_timeout_is_not_killed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Inside the verb's timeout, a step that ends by itself ends with its own status, untouched."""
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(os, 'killpg', lambda pgid, sig: sent.append((pgid, sig)))
    done = tmp_path / 'done'
    step = stack.Step((_BASH, '-c', f'sleep 0.3; touch {done}; exit 0'), tmp_path)

    async def scenario() -> runner.ProcessResult:
        async with asyncio.timeout(5):
            return await runner.run_process(step)

    assert _run_scenario(scenario()).exit_status == 0
    assert done.exists() and sent == []
