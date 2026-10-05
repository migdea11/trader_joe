"""Scaffolding for the agent-stack MCP server tests (tj-c4mosr.5): a tmp layout, fake git, fake docker.

Kept free of test functions, and not a conftest.py, so every test module imports exactly what it uses.

THE PR GATE RUNS NO DOCKER (tj-j4wknb R4: mock libraries are fine in-process). The server's two
subprocess routes are replaced here:
    stack.run_git        -> FakeGit: answers `worktree list --porcelain` and `cat-file blob HEAD:<path>`
                            and FAILS the test on any other git command.
    AgentStack(run=...)  -> FakeDocker: records every Step and answers from a table; nothing reaches a
                            daemon. forbid_real_subprocesses() additionally makes subprocess.run and
                            asyncio.create_subprocess_exec raise, so a code path that bypassed the
                            fakes would fail loudly instead of trying a real binary.

What only a daemon can show -- a real stack, the Host check in server.py (421), the SDK wiring -- is
the host sitting's (tj-c4mosr.6 H1-H3), not this suite's.
"""

import asyncio
import hashlib
import json
import os
import stat
import subprocess
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from tools.agent_mcp import runner, stack
from tools.agent_mcp.settings import Settings


REPO_ROOT = Path(__file__).resolve().parents[3]

WORKTREE_NAME = 'wt-one'

# One file (or more) under every SNAPSHOT_SOURCES entry, so a snapshot of this tree is complete.
WORKTREE_FILES = {
    'pyproject.toml': '[project]\nname = "fixture"\n',
    'uv.lock': 'version = 1\n',
    'entrypoint.sh': '#!/bin/sh\nexec "$@"\n',
    'common/__init__.py': '',
    'common/sub/module.py': 'VALUE = 1\n',
    'common/.env.default': 'COMMITTED_TEMPLATE=1\n',
    'routers/__init__.py': '',
    'schemas/__init__.py': '',
    # The committed generated gRPC tree the Dockerfile COPYs and compose mounts (decision tj-3mk3u5.42
    # F1). trader_joe/ itself has no __init__.py, as in the real tree: it is a PEP 420 namespace.
    'gen/proto/python/trader_joe/proto/__init__.py': '# guard\n',
    'gen/proto/python/trader_joe/proto/ping/v1/ping_pb2.py': 'DESCRIPTOR = None\n',
    'data/store/app/main.py': 'APP = "store"\n',
    'data/ingest/app/main.py': 'APP = "ingest"\n',
    'data/store/alembic.ini': '[alembic]\n',
    'data/store/migrations/env.py': 'ENV = 1\n',
    'data/store/migrations/versions/0001_initial.py': 'revision = "0001"\n',
    # The seed producer, test_client's read-only mount (ADR tj-4rr0la addendum 10 (2); tj-irhy0a.22).
    'data/store/seeds/__init__.py': '',
    'data/store/seeds/__main__.py': 'MAIN = 1\n',
    'tests/system/test_one.py': 'def test_one():\n    pass\n',
    'tests/system/sub/test_two.py': 'def test_two():\n    pass\n',
    # The fake-mode overlay's read-only mount source (docker-compose.fake.yaml; tj-vhboky.61).
    'tests/fakes/__init__.py': '',
    'tests/fakes/ingest_launcher.py': 'app = None\n',
    'pytest.ini': '[pytest]\n',
    # Outside every SNAPSHOT_SOURCES entry: never copied. tests/ is allow-listed per subdirectory,
    # never whole, so a sibling of tests/system and tests/fakes stays out.
    'Dockerfile': 'FROM scratch\n',
    'README.md': 'not a source\n',
    'tests/README.md': 'not a source\n',
}
EXECUTABLE_FILES = ('entrypoint.sh',)
# A live env file's content: it must never reach a snapshot, a generated file or any output.
LIVE_ENV_SENTINEL = 'LIVE_ENV_SENTINEL_d41d8cd98f00'


def build_worktree(path: Path) -> Path:
    """Write WORKTREE_FILES under PATH (entrypoint.sh executable) and return PATH."""
    for relative, text in WORKTREE_FILES.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    for relative in EXECUTABLE_FILES:
        (path / relative).chmod(0o755)
    return path


def plant_live_env_files(worktree: Path) -> list[Path]:
    """Write live env files (.env, data/store/.env, common/.env.local) holding LIVE_ENV_SENTINEL."""
    planted = []
    for relative in ('.env', 'data/store/.env', 'data/ingest/.env', 'common/.env', 'common/.env.local'):
        target = worktree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f'POSTGRES_PASS={LIVE_ENV_SENTINEL}\nALPACA_API_KEY={LIVE_ENV_SENTINEL}\n')
        planted.append(target)
    return planted


@dataclass
class Layout:
    """A repository with one agent worktree, the agent stack's directory and the share directory."""

    root: Path
    repo: Path
    worktree: Path
    stack_dir: Path
    home: Path

    @property
    def settings(self) -> Settings:
        return Settings(
            repo_root=self.repo,
            stack_dir=self.stack_dir,
            token_file=self.home / 'agent_mcp_token',
            bind='0.0.0.0',
            port=8765,
            hostname='agent_mcp',
        )

    @property
    def snapshot(self) -> Path:
        return self.stack_dir / stack.SOURCE_DIR_NAME

    @property
    def data_dir(self) -> Path:
        return self.stack_dir / stack.DATA_DIR_NAME


def make_layout(tmp_path: Path) -> Layout:
    """repo/ (the main checkout), repo/.claude/worktrees/wt-one, stack/ (0700) and share/, all real."""
    root = Path(os.path.realpath(tmp_path))
    repo = build_worktree(root / 'repo')
    worktree = build_worktree(repo / '.claude' / 'worktrees' / WORKTREE_NAME)
    stack_dir = root / 'stack'
    stack_dir.mkdir(mode=0o700)
    home = root / 'share'
    home.mkdir(mode=0o700)
    return Layout(root=root, repo=repo, worktree=worktree, stack_dir=stack_dir, home=home)


def committed_defaults() -> dict[str, str]:
    """The three committed .env.default files, as git cat-file would return them at HEAD."""
    return {source: (REPO_ROOT / source).read_text(encoding='utf-8') for source in stack.ENV_DEFAULT_SOURCES.values()}


def porcelain(paths: Sequence[Path]) -> str:
    """`git worktree list --porcelain` for PATHS, the main checkout first."""
    blocks = [f'worktree {path}\nHEAD {"0" * 40}\nbranch refs/heads/b{index}' for index, path in enumerate(paths)]
    return '\n\n'.join(blocks) + '\n'


@dataclass
class FakeGit:
    """stack.run_git's stand-in: the two plumbing commands the server may run, and nothing else."""

    worktree_paths: list[Path]
    defaults: dict[str, str] = field(default_factory=committed_defaults)
    delay: float = 0.0
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, repo_root: Path, args: Sequence[str]) -> str:
        self.calls.append(tuple(args))
        if self.delay:
            # A thread-blocking sleep, as a slow real git would block the thread that runs it.
            time.sleep(self.delay)
        if list(args) == ['worktree', 'list', '--porcelain']:
            return porcelain(self.worktree_paths)
        if len(args) == 3 and list(args[:2]) == ['cat-file', 'blob'] and args[2].startswith('HEAD:'):
            source = args[2].removeprefix('HEAD:')
            assert source in self.defaults, f'git cat-file of {source!r}, which is not a committed .env.default'
            return self.defaults[source]
        raise AssertionError(f'the server ran a git command outside its two plumbing calls: {list(args)}')

    @property
    def list_calls(self) -> int:
        return self.calls.count(('worktree', 'list', '--porcelain'))


# What the seed producer prints on success: one bundle line (decision tj-vhboky.55 S9), spelled here
# from literals rather than by either reader's code, so a sample seed_dump call ends 'ok'.
SEED_REVISION = '0a1b2c3d4e5f'
SEED_SQL = "INSERT INTO public.store_dataset_entry (id) VALUES ('harness');\n"
SEED_MANIFEST = json.dumps({'revision': SEED_REVISION, 'row_counts': {'store_dataset_entry': 1}}) + '\n'
SEED_STDOUT = (
    json.dumps({'bundle': 'trader_joe-seed/1', 'revision': SEED_REVISION, 'sql': SEED_SQL, 'manifest': SEED_MANIFEST})
    + '\n'
).encode('utf-8')


def is_seed_producer(step: stack.Step) -> bool:
    """The step that runs the seed producer in test_client (its stdout is the bundle)."""
    return 'data.store.seeds' in step.argv


def default_response(step: stack.Step) -> runner.ProcessResult:
    """Every docker step succeeds; `ps -q postgres` answers with one container id (postgres is running).

    The seed producer's run answers with SEED_STDOUT, a valid bundle.
    """
    if list(step.argv[-3:]) == ['ps', '-q', 'postgres']:
        return runner.ProcessResult(0, b'0123456789ab\n', b'')
    if is_seed_producer(step):
        return runner.ProcessResult(0, SEED_STDOUT, b'')
    return runner.ProcessResult(0, b'', b'')


@dataclass
class FakeDocker:
    """AgentStack's `run`: records every Step, runs an optional hook, and answers from `respond`."""

    respond: Callable[[stack.Step], runner.ProcessResult] = default_response
    hook: Callable[[stack.Step], Awaitable[None] | None] | None = None
    steps: list[stack.Step] = field(default_factory=list)

    async def __call__(self, step: stack.Step) -> runner.ProcessResult:
        self.steps.append(step)
        if self.hook is not None:
            outcome = self.hook(step)
            if asyncio.iscoroutine(outcome):
                await outcome
        return self.respond(step)

    def subcommands(self) -> list[str]:
        """The compose subcommand of every recorded step, after the fixed prefix."""
        return [step.argv[step_prefix_length()] for step in self.steps]


def step_prefix_length() -> int:
    """How many argv words compose_prefix() spells before the subcommand."""
    return len(stack.compose_prefix(Path('/stack'), Path('/stack/agent_stack.env')))


def dev_step_prefix_length() -> int:
    """How many argv words dev_compose_prefix() spells before the subcommand (four: no -f, no env file)."""
    return len(stack.dev_compose_prefix())


def forbid_real_subprocesses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any real subprocess from the server under test fails the test: the fakes must see everything."""

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(f'a real subprocess was started: {args!r}')

    async def refuse_async(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(f'a real subprocess was started: {args!r}')

    monkeypatch.setattr(subprocess, 'run', refuse)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', refuse_async)


@dataclass
class Rig:
    """One layout with its fakes installed and an AgentStack over them."""

    layout: Layout
    git: FakeGit
    docker: FakeDocker
    agent: runner.AgentStack

    def call(self, verb: str, arguments: object) -> dict[str, Any]:
        return asyncio.run(self.agent.call(verb, arguments))

    def audit_lines(self) -> list[str]:
        path = self.layout.stack_dir / stack.AUDIT_LOG_NAME
        return path.read_text().splitlines() if path.exists() else []


def make_rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, docker: FakeDocker | None = None) -> Rig:
    """A layout, FakeGit listing repo and its one agent worktree, FakeDocker, and no real subprocess."""
    layout = make_layout(tmp_path)
    git = FakeGit(worktree_paths=[layout.repo, layout.worktree])
    monkeypatch.setattr(stack, 'run_git', git)
    forbid_real_subprocesses(monkeypatch)
    fake_docker = docker or FakeDocker()
    return Rig(layout=layout, git=git, docker=fake_docker, agent=runner.AgentStack(layout.settings, run=fake_docker))


def populate_data(layout: Layout) -> None:
    """What a running agent stack leaves under data/: a file (and a dotfile) in each service mount."""
    for service, _ in stack.DATA_MOUNTS:
        directory = layout.data_dir / service
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'PG_VERSION').write_text('17\n')
        (directory / '.hidden').write_text('state\n')


def clearing_hook(layout: Layout) -> Callable[[stack.Step], None]:
    """A FakeDocker hook doing what the in-container clear step does: empty the service's data mount.

    It maps the step's container target back to the host directory through DATA_MOUNTS, as the
    overlay's `${DATA_DIR}/<service>:<target>` bind does, and deletes that directory's entries.
    """
    by_target = {target: service for service, target in stack.DATA_MOUNTS}

    def hook(step: stack.Step) -> None:
        argv = list(step.argv)
        if 'run' in argv and '--entrypoint' in argv and argv[-2] == 'clear':
            host = layout.data_dir / by_target[argv[-1]]
            for entry in list(host.iterdir()):
                entry.unlink()

    return hook


def record_state(layout: Layout, name: str = WORKTREE_NAME) -> None:
    """What a successful stack_up leaves: the recorded worktree name (migrate reads it)."""
    (layout.stack_dir / stack.STATE_FILE_NAME).write_text(f'{name}\n')


# A valid call of every verb. A verb added to AgentStack without an entry here fails
# test_every_verb_has_a_sample, so parameterised tests cannot silently skip it.
VERB_SAMPLES: Mapping[str, dict[str, Any]] = {
    'stack_up': {'worktree': WORKTREE_NAME},
    'stack_down': {},
    'stack_wipe': {},
    'migrate': {},
    'migrate_status': {},
    'migrate_check': {},
    'run_system_tests': {'worktree': WORKTREE_NAME, 'paths': ['tests/system/test_one.py']},
    'seed_dump': {'worktree': WORKTREE_NAME},
    'logs': {'service': 'postgres', 'tail': 50},
    'ps': {},
    'dev_logs': {'service': 'postgres', 'tail': 50},
    'dev_ps': {},
}
# Verbs that run no docker at all. None since tj-irhy0a.22 wired seed_dump; kept so a future verb
# that runs no docker is classified here rather than tripping the docker sweeps.
DOCKERLESS_VERBS: frozenset[str] = frozenset()
# The verbs that reach the USER'S OWN dev compose project read-only (tj-kzy7w2; ADR tj-4rr0la
# addendum 18). They run docker, but NOT through stack.compose_prefix(): no -f, no
# --project-directory, no --env-file, so the agent-stack sweeps must exclude them rather than
# mis-slice their four-word prefix. tj-tq2hn6 (B2) owns the containment tests that take their place.
DEV_PROJECT_VERBS = frozenset({'dev_ps', 'dev_logs'})


def tree_digest(path: Path) -> dict[str, str]:
    """Relative path -> 'dir' or the sha256 of the file, for a byte-identical comparison of two trees."""
    digest = {}
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in dirnames:
            digest[os.path.relpath(os.path.join(dirpath, name), path)] = 'dir'
        for name in filenames:
            full = os.path.join(dirpath, name)
            if stat.S_ISLNK(os.lstat(full).st_mode):
                digest[os.path.relpath(full, path)] = f'link->{os.readlink(full)}'
            else:
                digest[os.path.relpath(full, path)] = hashlib.sha256(Path(full).read_bytes()).hexdigest()
    return digest
