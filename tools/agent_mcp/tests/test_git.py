"""run_git against a REAL git and a real repository: the two plumbing calls, and nothing executed.

tj-c4mosr.5 item D3 (01:40 (6)): an agent can write this repository's .git/config, and a key like
core.fsmonitor makes git EXECUTE a command -- inside the one container that holds Docker access. The
argv and environment are pinned in test_commands.py; this module shows the behaviour on real git.

EQUIVALENT MUTANT, STATED: `git worktree list` and `git cat-file blob` never consult core.fsmonitor
or run a hook (checked on git 2.39: `git status` fires the fsmonitor sentinel, these two do not). So
dropping `-c core.fsmonitor=false` or `-c core.hooksPath=/dev/null` does NOT turn the sentinel test
red -- the flags are defence in depth for these commands, and their presence is pinned by
test_run_git_is_hardened_plumbing_with_a_fixed_environment instead. The sentinel test stays: it
fails the day a third git command that does consult them is added without the hardening.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from tools.agent_mcp import stack


pytestmark = pytest.mark.build_infra

GIT = shutil.which('git')


def _git(cwd: Path, *args: str) -> str:
    assert GIT, 'git is not on PATH'
    command = [GIT, '-c', 'user.name=validator', '-c', 'user.email=validator@example.invalid', *args]
    return subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path.resolve() / 'repo'
    root.mkdir()
    _git(root, 'init', '-q', '-b', 'main')
    for source in stack.ENV_DEFAULT_SOURCES.values():
        (root / source).parent.mkdir(parents=True, exist_ok=True)
        (root / source).write_text(f'COMMITTED_{source.replace("/", "_").replace(".", "_")}=1\n')
    _git(root, 'add', '.')
    _git(root, 'commit', '-qm', 'defaults')
    _git(root, 'worktree', 'add', '-q', '-b', 'wt', str(root / '.claude' / 'worktrees' / 'wt-real'))
    return root


def _plant_executing_config(repo: Path) -> Path:
    sentinel = repo.parent / 'SENTINEL'
    hook = repo.parent / 'hook.sh'
    hook.write_text(f'#!/bin/sh\ntouch {sentinel}\n')
    hook.chmod(0o755)
    hooks_dir = repo.parent / 'hooks'
    hooks_dir.mkdir()
    for name in ('post-checkout', 'reference-transaction', 'pre-auto-gc'):
        (hooks_dir / name).symlink_to(hook)
    _git(repo, 'config', 'core.fsmonitor', str(hook))
    _git(repo, 'config', 'core.hooksPath', str(hooks_dir))
    _git(repo, 'config', 'core.pager', str(hook))
    return sentinel


def test_the_planted_config_does_execute_for_a_porcelain_command(repo: Path):
    """Guard the guard: the sentinel is live -- an ordinary `git status` does run the planted fsmonitor."""
    sentinel = _plant_executing_config(repo)
    _git(repo, 'status')
    assert sentinel.exists(), 'the planted fsmonitor never ran, so the test below would prove nothing'


def test_the_plumbing_calls_execute_nothing_and_answer_correctly(repo: Path):
    sentinel = _plant_executing_config(repo)
    worktrees = stack.list_worktrees(repo)
    assert worktrees == {'root': repo, 'wt-real': repo / '.claude' / 'worktrees' / 'wt-real'}
    (repo / '.env.default').write_text('UNCOMMITTED=1\n')
    defaults = stack.read_committed_defaults(repo)
    assert defaults['root'] == {'COMMITTED__env_default': '1'}, (
        'the committed defaults are read from HEAD, not the tree'
    )
    assert set(defaults) == {'root', 'store', 'ingest'}
    assert not sentinel.exists(), 'a git call made by the server executed a configured command'


def test_a_git_failure_is_a_refusal_naming_the_command(tmp_path: Path):
    with pytest.raises(stack.Refused, match='git worktree failed'):
        stack.run_git(tmp_path, ['worktree', 'list', '--porcelain'])


def test_a_prunable_worktree_and_a_duplicate_name_are_left_out(repo: Path):
    gone = repo / '.claude' / 'worktrees' / 'wt-gone'
    _git(repo, 'worktree', 'add', '-q', '-b', 'gone', str(gone))
    shutil.rmtree(gone)
    other = repo / 'elsewhere' / 'wt-real'
    _git(repo, 'worktree', 'add', '-q', '-b', 'dup', str(other))
    assert stack.list_worktrees(repo) == {'root': repo}
