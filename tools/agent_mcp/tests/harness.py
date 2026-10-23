"""Scaffolding for the agent-stack MCP server tests (tj-c4mosr.5): a tmp layout, fake git, fake docker.

Kept free of test functions, and not a conftest.py, so every test module imports exactly what it uses.

THE PR GATE RUNS NO DOCKER (tj-j4wknb R4: mock libraries are fine in-process). The server's two
subprocess routes are replaced here:
    stack.run_git        -> FakeGit: answers `worktree list --porcelain` and FAILS the test on any
                            other git command (the committed-defaults `cat-file` went with the agent
                            stack's generated env files, tj-iee6sn.21).
    AgentStack(run=...)  -> FakeDocker: records every Step and answers from a table; nothing reaches a
                            daemon. forbid_real_subprocesses() additionally makes subprocess.run and
                            asyncio.create_subprocess_exec raise, so a code path that bypassed the
                            fakes would fail loudly instead of trying a real binary.

What only a daemon can show -- a real stack, the Host check in server.py (421), the SDK wiring -- is
the host sitting's (tj-c4mosr.6 H1-H3), not this suite's.
"""

import asyncio
import functools
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


# THE TWO ROOTS, DERIVED LOCALLY AND DELIBERATELY NOT IMPORTED (tj-iontkq.2).
#
# The repository root and the server root are NOT the same directory: the service trees sit under
# server/ while tools/, tests/ and the build files stay at the top. One counted parents[3] called
# REPO_ROOT served both only while the two roots were the same directory.
#
# WHY THIS DOES NOT IMPORT common/tests/roots.py, which is the canonical definition of both
# sentinels. Two reasons, and both are about the seam this module sits on:
#   * tools/ stays at the top of the repository while common/ travels down, so `make test PATHS=tools`
#     would have only the repository root on sys.path and `import common.tests.roots` would be an
#     ImportError -- a whole-tree run would pass and a tools-scoped run would not collect at all.
#   * the upward marker search cannot find the server root from here anyway: from
#     tools/agent_mcp/tests the ancestors are tools/agent_mcp, tools and the repository root, and the
#     server root is none of those once it is a sibling of tools/ rather than the root itself.
# So the server root is found by stepping DOWN from the repository root instead -- the root itself if
# it carries the three shared trees, otherwise the one child of it that does. Both raise rather than
# fall back, for the reason roots.py gives: a sentinel that quietly returns the wrong directory makes
# every file it names missing and leaves the assertions about them green.
#
# EACH MARKER MUST BE AN IMPORTABLE PACKAGE, NOT MERELY A DIRECTORY OF THAT NAME (bug tj-fts1lo).
# THIS is the search that collapsed, and the downward direction is why: it tries the repository root
# FIRST, so anything at the root answering the marker wins over the real server/ below it. `git mv`
# relocates TRACKED files only, so every checkout that predates tj-iontkq.4 -- the user's, any
# developer's, never CI's, which builds fresh -- still holds common/, routers/ and schemas/ at the
# repository root containing nothing but untracked __pycache__. Those empty shells answered
# `is_dir()`, _server_root returned the repository root, and the two roots collapsed back into the
# single root this epic spent four tasks separating. It surfaced as one loud FileNotFoundError in
# test_seed_dump, but the collapse restores the PRE-MOVE value, so every other caller that merely
# needs *a* root went on passing -- vacuously. The leftovers are untracked, so nothing in version
# control removes them and no clean checkout reproduces the condition: the marker has to
# discriminate. None of the shells carries an __init__.py.
#
# roots.py keys on the same package marker, and so do the other two spellings of this same pair --
# tests/fakes/record_alpaca.py (same downward shape, same direction, silent when it collapses) and,
# in shell, server/data/store/run_migrations.sh. FOUR spellings, each forced by where it sits, so
# common/tests/test_roots.py::test_every_derivation_of_the_two_roots_agrees enumerates all four and
# holds each to the canonical one. A definition spelled in several places is only as strong as its
# weakest spelling, and only an inventory can say how many places there are.

SERVER_MARKERS = ('common', 'routers', 'schemas')
PACKAGE_MARKER = '__init__.py'


def _repo_root() -> Path:
    """The nearest ancestor of this file containing pytest.ini -- the true repository root."""
    start = Path(__file__).resolve().parent
    for candidate in (start, *start.parents):
        if (candidate / 'pytest.ini').is_file():
            return candidate
    raise RuntimeError(f'no ancestor of {start} carries pytest.ini, so the repository root cannot be located')


def _server_root(repo_root: Path) -> Path:
    """The directory holding every SERVER_MARKERS name as a PACKAGE: REPO_ROOT, or its one child."""
    candidates = [repo_root, *sorted(child for child in repo_root.iterdir() if child.is_dir())]
    for candidate in candidates:
        if all((candidate / marker / PACKAGE_MARKER).is_file() for marker in SERVER_MARKERS):
            return candidate
    raise RuntimeError(
        f'neither {repo_root} nor any child of it holds all of {SERVER_MARKERS} as packages (<name>/{PACKAGE_MARKER})'
    )


REPO_ROOT = _repo_root()
SERVER_ROOT = _server_root(REPO_ROOT)


WORKTREE_NAME = 'wt-one'

# One file (or more) under every SNAPSHOT_SOURCES entry, so a snapshot of this tree is complete.
# The fixture mirrors the real worktree's layout, so the four service trees sit under server/ since
# epic tj-iontkq.4 and the root siblings -- gen/, tests/, pytest.ini and the three build files -- do
# not. A key that lost its prefix would sit under no SNAPSHOT_SOURCES entry and silently stop being
# copied, which is the completeness this dict exists to assert.
WORKTREE_FILES = {
    'pyproject.toml': '[project]\nname = "fixture"\n',
    'uv.lock': 'version = 1\n',
    'entrypoint.sh': '#!/bin/sh\nexec "$@"\n',
    'server/common/__init__.py': '',
    'server/common/sub/module.py': 'VALUE = 1\n',
    'server/common/.env.default': 'COMMITTED_TEMPLATE=1\n',
    'server/routers/__init__.py': '',
    'server/schemas/__init__.py': '',
    # The committed generated gRPC tree the Dockerfile COPYs and compose mounts (decision tj-3mk3u5.42
    # F1). trader_joe/ itself has no __init__.py, as in the real tree: it is a PEP 420 namespace.
    'gen/proto/python/trader_joe/proto/__init__.py': '# guard\n',
    'gen/proto/python/trader_joe/proto/ping/v1/ping_pb2.py': 'DESCRIPTOR = None\n',
    'server/data/store/app/main.py': 'APP = "store"\n',
    'server/data/ingest/app/main.py': 'APP = "ingest"\n',
    'server/data/store/alembic.ini': '[alembic]\n',
    'server/data/store/migrations/env.py': 'ENV = 1\n',
    'server/data/store/migrations/versions/0001_initial.py': 'revision = "0001"\n',
    # The seed producer, test_client's read-only mount (ADR tj-4rr0la addendum 10 (2); tj-irhy0a.22).
    'server/data/store/seeds/__init__.py': '',
    'server/data/store/seeds/__main__.py': 'MAIN = 1\n',
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
# A live env file's content: it must never reach a snapshot, the audit log or any output.
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
    """Write live env files (.env, server/data/store/.env, server/common/.env.local) holding LIVE_ENV_SENTINEL."""
    planted = []
    for relative in (
        '.env',
        'server/data/store/.env',
        'server/data/ingest/.env',
        'server/common/.env',
        'server/common/.env.local',
    ):
        target = worktree / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f'POSTGRES_PASS={LIVE_ENV_SENTINEL}\nALPACA_API_KEY={LIVE_ENV_SENTINEL}\n')
        planted.append(target)
    return planted


@dataclass
class Layout:
    """A repository with one agent worktree, the MCP's stack directory and the share directory."""

    root: Path
    repo: Path
    worktree: Path
    stack_dir: Path
    home: Path
    # The root checkout at its HOST path (ADR tj-4rr0la addendum 23; tj-iee6sn.5): in agent_mcp the
    # same tree bind-mounted read-only a second time. Here a symlink to repo/, so the string differs
    # from repo_root (a verb that names the wrong one is visible in its argv) while every file under
    # it is the same file.
    repo_host_path: Path

    @property
    def settings(self) -> Settings:
        return Settings(
            repo_root=self.repo,
            repo_host_path=self.repo_host_path,
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
    def trusted_dir(self) -> Path:
        """The stand-in for /opt/agent_mcp/compose that make_rig points the S6 check at."""
        return self.root / TRUSTED_DIR_NAME


# build (tj-iee6sn.8) refuses unless the generated code is no older than the newest .proto, read
# through the repository root. The ROOT checkout gets one generated TypeScript file and one .proto an
# hour older than everything else, so a sample build passes the precondition. Only the root
# checkout: no worktree, and so no snapshot, holds them.
DEV_BUILD_ROOT_FILES = {
    'gen/proto/ts/trader_joe/proto/ping/v1/ping_pb.ts': 'export {};\n',
    'proto/trader_joe/proto/ping/v1/ping.proto': 'syntax = "proto3";\n',
}
DEV_BUILD_PROTO_AGE_SECONDS = 3600
# The TRUSTED directory a rig's build compares the checkout with (S6), in place of the image's
# /opt/agent_mcp/compose, which no test machine has. make_layout writes every stack.DEV_TRUSTED_COPIES
# file into BOTH it and the root checkout, identical, so a sample call passes the stale-copy check.
TRUSTED_DIR_NAME = 'trusted'
# What a digest step prints in the rig: one 64-hex line per service, distinct and from literals.
DEV_DIGESTS = {('data', 'store'): '1' * 64, ('data', 'ingest'): '2' * 64}
TRUSTED_SOURCE_DIGEST_SCRIPT = '/opt/agent_mcp/compose/tools/source_digest.sh'


def is_dev_digest(step: stack.Step) -> bool:
    """The trusted source_digest.sh run by bash: build's only argv that is not docker."""
    return list(step.argv[:2]) == ['/usr/bin/bash', TRUSTED_SOURCE_DIGEST_SCRIPT]


def write_dev_build_fixtures(root: Path, repo: Path) -> Path:
    """The generated-code files and the identical trusted copies build needs; returns the trusted directory."""
    for relative, text in DEV_BUILD_ROOT_FILES.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    proto = repo / 'proto/trader_joe/proto/ping/v1/ping.proto'
    old = time.time() - DEV_BUILD_PROTO_AGE_SECONDS
    os.utime(proto, (old, old))
    trusted = root / TRUSTED_DIR_NAME
    for checkout_name, baked_name in stack.DEV_TRUSTED_COPIES:
        source = repo / checkout_name
        if not source.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(f'# fixture copy of {checkout_name}\n')
        target = trusted / baked_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    return trusted


def make_layout(tmp_path: Path) -> Layout:
    """repo/ (the main checkout), repo/.claude/worktrees/wt-one, stack/ (0700), share/, and host/repo -> repo."""
    root = Path(os.path.realpath(tmp_path))
    repo = build_worktree(root / 'repo')
    write_dev_build_fixtures(root, repo)
    worktree = build_worktree(repo / '.claude' / 'worktrees' / WORKTREE_NAME)
    stack_dir = root / 'stack'
    stack_dir.mkdir(mode=0o700)
    home = root / 'share'
    home.mkdir(mode=0o700)
    host = root / 'host'
    host.mkdir()
    repo_host_path = host / 'repo'
    repo_host_path.symlink_to(repo, target_is_directory=True)
    return Layout(
        root=root, repo=repo, worktree=worktree, stack_dir=stack_dir, home=home, repo_host_path=repo_host_path
    )


def porcelain(paths: Sequence[Path]) -> str:
    """`git worktree list --porcelain` for PATHS, the main checkout first."""
    blocks = [f'worktree {path}\nHEAD {"0" * 40}\nbranch refs/heads/b{index}' for index, path in enumerate(paths)]
    return '\n\n'.join(blocks) + '\n'


@dataclass
class FakeGit:
    """stack.run_git's stand-in: the one plumbing command the server may run, and nothing else."""

    worktree_paths: list[Path]
    delay: float = 0.0
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def __call__(self, repo_root: Path, args: Sequence[str]) -> str:
        self.calls.append(tuple(args))
        if self.delay:
            # A thread-blocking sleep, as a slow real git would block the thread that runs it.
            time.sleep(self.delay)
        if list(args) == ['worktree', 'list', '--porcelain']:
            return porcelain(self.worktree_paths)
        raise AssertionError(f'the server ran a git command outside its one plumbing call: {list(args)}')

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


# The dev exec family (ADR tj-4rr0la addendum 22): the label lookup is a plain `docker ps` whose
# last two words are `--format {{.ID}}`, and it answers with exactly one running container, so a
# sample db_tables call ends 'ok'. Spelled from literals, not from stack.dev_lookup_argv().
DEV_CONTAINER_ID = 'c0ffee000001'
DEV_DB_TABLES_STDOUT = (
    b'dataset_read|t\ndataset_read_daily|t\ndataset_override_log|t\ndataset_deletion|t\n'
    b'ingest_run|t\nstore_dataset_entry|t\nstock_market_activity|t\nalembic_version|t\n'
    b'version_num|d7e2a9c35b18\n'
)
# The data_ingest container's id in the rig: distinct from DEV_CONTAINER_ID so that migrate's TEST-mode stamp
# gate (tj-iee6sn.10), which reads each RUNNING data_store and data_ingest container by id, can be answered
# per service. No other verb looks data_ingest up.
DEV_INGEST_CONTAINER_ID = 'c0ffee000002'
# What migrate_status' exec script prints (tj-iee6sn.13.5, folded into migrate_status by tj-iee6sn.10): a header per alembic subcommand, then
# its redacted output. Told apart from db_tables' exec by its option set, `-w /code` (the table of
# stack.DEV_EXEC_COMMANDS), spelled here from literals.
DEV_MIGRATE_STATUS_STDOUT = (
    b'== alembic current ==\nd7e2a9c35b18 (head)\n'
    b'== alembic history ==\nb3d9e1a47c62 -> d7e2a9c35b18 (head), dataset override log and deletion\n'
)
# What the http_get script prints for /ping (tj-iee6sn.13.6): the status line, then the redacted
# body. Told apart by its option set, which is NONE: the container id follows `exec` directly.
DEV_HTTP_GET_STDOUT = b'== GET /ping -> HTTP 200 ==\n{"message":"pong"}\n'


def is_dev_lookup(step: stack.Step) -> bool:
    """A plain `docker ps ... --format {{.ID}}`: the dev exec target lookup."""
    return list(step.argv[:2]) == ['/usr/local/bin/docker', 'ps'] and list(step.argv[-2:]) == ['--format', '{{.ID}}']


def is_dev_exec(step: stack.Step) -> bool:
    """A plain `docker exec`: the dev exec family's only other argv."""
    return list(step.argv[:2]) == ['/usr/local/bin/docker', 'exec']


def is_dev_stamp_read(step: stack.Step) -> bool:
    """A `docker image inspect` printing one image's source-stamp label (test_up reads both :test images)."""
    return (
        list(step.argv[:4]) == ['/usr/local/bin/docker', 'image', 'inspect', '--format']
        and 'trader_joe.source.digest' in step.argv[4]
    )


def is_container_stamp_read(step: stack.Step) -> bool:
    """A `docker inspect --type container` printing a RUNNING container's source-stamp label (migrate's TEST gate)."""
    return (
        list(step.argv[:5]) == ['/usr/local/bin/docker', 'inspect', '--type', 'container', '--format']
        and 'trader_joe.source.digest' in step.argv[5]
    )


def is_mode_guard(step: stack.Step) -> bool:
    """The mode guard's `docker ps -a ... --format {{.Label "...config_files"}}`."""
    return list(step.argv[:3]) == ['/usr/local/bin/docker', 'ps', '-a'] and 'config_files' in step.argv[-1]


# The config_files label of a container `make dev-up` started: the override present, the fake file absent.
# The rig's default project is in DEV mode, so a sample of every guarded verb is admitted, migrate and
# migrate_status included (they refuse an empty project, tj-iee6sn.10). Spelled from literals.
DEV_MODE_LINE = b'/r/docker-compose.yaml,/r/docker-compose.override.yaml,/r/docker-compose.web.yaml\n'


def default_response(step: stack.Step) -> runner.ProcessResult:
    """Every docker step succeeds; `ps -q postgres` answers with one container id (postgres is running).

    The seed producer's run answers with SEED_STDOUT, a valid bundle. The dev lookup answers with
    one id (DEV_INGEST_CONTAINER_ID for data_ingest, else DEV_CONTAINER_ID) and a dev exec with
    DEV_DB_TABLES_STDOUT, the shape the db_tables script prints, or, for the `-w /code` exec of
    migrate_status, DEV_MIGRATE_STATUS_STDOUT, or, for the option-less exec of http_get,
    DEV_HTTP_GET_STDOUT. The mode guard's `docker ps` answers with one DEV container (DEV_MODE_LINE),
    which every guarded verb accepts, so a sample down/restart/migrate ends 'ok'. build's
    digest steps answer with their service's DEV_DIGESTS line, so a sample build ends 'ok'. An
    image stamp read answers with its own service's digest (a current image), and a container stamp
    read with the digest of the service whose id the lookup gave (a current running container).
    """
    if list(step.argv[-3:]) == ['ps', '-q', 'postgres']:
        return runner.ProcessResult(0, b'0123456789ab\n', b'')
    if is_mode_guard(step):
        return runner.ProcessResult(0, DEV_MODE_LINE, b'')
    if is_dev_digest(step):
        return runner.ProcessResult(0, f'{DEV_DIGESTS[tuple(step.argv[-2:])]}\n'.encode(), b'')
    if is_dev_stamp_read(step):
        # Each image's stamp is its own service's digest (tj-iee6sn.24: test_up reads both :test images).
        service = ('data', 'ingest') if step.argv[-1].startswith('trader_joe_data_ingest:') else ('data', 'store')
        return runner.ProcessResult(0, f'{DEV_DIGESTS[service]}\n'.encode(), b'')
    if is_container_stamp_read(step):
        service = ('data', 'ingest') if step.argv[-1] == DEV_INGEST_CONTAINER_ID else ('data', 'store')
        return runner.ProcessResult(0, f'{DEV_DIGESTS[service]}\n'.encode(), b'')
    if is_seed_producer(step):
        return runner.ProcessResult(0, SEED_STDOUT, b'')
    if is_dev_lookup(step):
        ingest = 'label=com.docker.compose.service=data_ingest' in step.argv
        return runner.ProcessResult(0, f'{DEV_INGEST_CONTAINER_ID if ingest else DEV_CONTAINER_ID}\n'.encode(), b'')
    if is_dev_exec(step) and list(step.argv[2:4]) == ['-w', '/code']:
        return runner.ProcessResult(0, DEV_MIGRATE_STATUS_STDOUT, b'')
    if is_dev_exec(step) and len(step.argv) > 2 and not step.argv[2].startswith('-'):
        return runner.ProcessResult(0, DEV_HTTP_GET_STDOUT, b'')
    if is_dev_exec(step):
        return runner.ProcessResult(0, DEV_DB_TABLES_STDOUT, b'')
    return runner.ProcessResult(0, b'', b'')


# THE SYSTEM-SUITE PAIR (tj-iee6sn.20; ADR tj-4rr0la addenda 26-27). run_system_tests and seed_dump act on
# TEST mode ONLY, and only once the running postgres's data mount is the test directory, so the rig's DEV
# default refuses them. system_suite_response(layout) is the answer under which a sample of either ends 'ok':
# the mode guard sees one TEST container (base + fake, as make system-launch and test_up start it) and the
# postgres mount read prints <repo host path>/volume_data/test/postgres. Every other step falls through to
# BASE (default_response unless given), so the lookups and the stamps stay current. Spelled from literals.
SYSTEM_SUITE_VERBS = frozenset({'run_system_tests', 'seed_dump'})
TEST_MODE_LINE = b'/r/docker-compose.yaml,/r/docker-compose.fake.yaml\n'
POSTGRES_DATA_TARGET = '/var/lib/postgresql/data'


def is_postgres_mount_read(step: stack.Step) -> bool:
    """The rail's `docker inspect --type container --format <the postgres data mount's Source>` of one id."""
    return (
        list(step.argv[:5]) == ['/usr/local/bin/docker', 'inspect', '--type', 'container', '--format']
        and POSTGRES_DATA_TARGET in step.argv[5]
    )


def is_base_step(step: stack.Step) -> bool:
    """One of the ensure-bases pair (ADR tj-4rr0la addendum 14 (3)): the inspect or the pull of a BASE_IMAGES ref."""
    return any(
        tuple(step.argv)
        in (
            ('/usr/local/bin/docker', 'image', 'inspect', '--format', '{{.Id}}', ref),
            ('/usr/local/bin/docker', 'pull', ref),
        )
        for ref in stack.BASE_IMAGES
    )


def postgres_test_source(layout: Layout) -> str:
    """The only data mount source test mode's postgres has: <repo host path>/volume_data/test/postgres."""
    return f'{layout.repo_host_path}/volume_data/test/postgres'


def system_suite_response(
    layout: Layout, base: Callable[[stack.Step], runner.ProcessResult] = default_response
) -> Callable[[stack.Step], runner.ProcessResult]:
    """BASE, but the mode guard answers TEST_MODE_LINE and the mount read the test directory."""

    def respond(step: stack.Step) -> runner.ProcessResult:
        if is_mode_guard(step):
            return runner.ProcessResult(0, TEST_MODE_LINE, b'')
        if is_postgres_mount_read(step):
            return runner.ProcessResult(0, f'{postgres_test_source(layout)}\n'.encode(), b'')
        return base(step)

    return respond


def response_for(
    verb: str, layout: Layout, base: Callable[[stack.Step], runner.ProcessResult] = default_response
) -> Callable[[stack.Step], runner.ProcessResult]:
    """The answer under which VERB's sample is admitted: system_suite_response for the system-suite pair, else BASE."""
    return system_suite_response(layout, base) if verb in SYSTEM_SUITE_VERBS else base


# The client's command (tj-iee6sn.20; no shell wrapper since 5a38b7f), spelled from literals:
#   docker compose -p trader_joe --project-directory <stack>/source --env-file <host>/.env
#   -f <trusted>/docker-compose.yaml -f <trusted>/docker-compose.test-client.yaml -f <trusted>/docker-compose.agent-client.yaml
CLIENT_FILES = ('docker-compose.yaml', 'docker-compose.test-client.yaml', 'docker-compose.agent-client.yaml')


def client_prefix_words_for(stack_dir: Path, repo_host_path: Path) -> list[str]:
    """Every word of a client step before its tail, for STACK_DIR and REPO_HOST_PATH: compose's, unwrapped."""
    return [
        '/usr/local/bin/docker',
        'compose',
        '-p',
        'trader_joe',
        '--project-directory',
        f'{stack_dir}/source',
        '--env-file',
        f'{repo_host_path}/.env',
        *(word for name in CLIENT_FILES for word in ('-f', f'/opt/agent_mcp/compose/{name}')),
    ]


def client_prefix_words(layout: Layout) -> list[str]:
    """client_prefix_words_for the rig's stack directory and repository host path."""
    return client_prefix_words_for(layout.stack_dir, layout.repo_host_path)


def is_client_step(step: stack.Step) -> bool:
    """A step under the client's prefix: `docker compose -p trader_joe --project-directory D --env-file F`.

    The --env-file right after the project directory is what tells it from every other file-based compose step.
    """
    return (
        len(step.argv) > 7
        and list(step.argv[:5]) == ['/usr/local/bin/docker', 'compose', '-p', 'trader_joe', '--project-directory']
        and step.argv[6] == '--env-file'
    )


def client_tail(step: stack.Step, layout: Layout) -> list[str]:
    """The words of a client step after its whole fixed prefix (asserted, word for word)."""
    prefix = client_prefix_words(layout)
    assert list(step.argv[: len(prefix)]) == prefix, step.argv
    return list(step.argv[len(prefix) :])


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
    # The REAL S6 check, comparing with the layout's trusted directory instead of the image's.
    monkeypatch.setattr(
        stack,
        'check_trusted_copies_current',
        functools.partial(stack.check_trusted_copies_current, trusted_dir=layout.trusted_dir),
    )
    forbid_real_subprocesses(monkeypatch)
    fake_docker = docker or FakeDocker()
    return Rig(layout=layout, git=git, docker=fake_docker, agent=runner.AgentStack(layout.settings, run=fake_docker))


def budget_spent(rig: Rig) -> int:
    """How many state-changing calls the rig's S10 budget holds in its window (read in place, never charged)."""
    return len(rig.agent.dev_budget._calls)


# A valid call of every verb. A verb added to AgentStack without an entry here fails
# test_every_verb_has_a_sample, so parameterised tests cannot silently skip it. Exactly the fifteen
# verbs of ADR tj-4rr0la addenda 26 (5) and 27 (4), under today's names (the rename is tj-iee6sn.26).
VERB_SAMPLES: Mapping[str, dict[str, Any]] = {
    'migrate': {},
    'migrate_status': {},
    'run_system_tests': {'worktree': WORKTREE_NAME, 'paths': ['tests/system/test_one.py']},
    'seed_dump': {'worktree': WORKTREE_NAME},
    'logs': {'service': 'postgres', 'tail': 50},
    'ps': {},
    'db_tables': {},
    'http_get': {'name': 'ping'},
    'down': {},
    'restart': {'service': 'data_store'},
    'build': {},
    'up': {},
    'tools': {},
    'test_build': {},
    'test_up': {},
}
# Verbs that run no docker at all. None since tj-irhy0a.22 wired seed_dump; kept so a future verb
# that runs no docker is classified here rather than tripping the docker sweeps.
DOCKERLESS_VERBS: frozenset[str] = frozenset()
# The verbs that act on the one project WITHOUT the system-test client (tj-kzy7w2 on; ADR tj-4rr0la
# addenda 18 and 26): every verb but SYSTEM_SUITE_VERBS, which run test_client under the snapshot
# prefix (stack.testclient_prefix). With the agent stack gone (tj-iee6sn.21) the two sets partition
# VERB_SAMPLES; test_commands.py pins that. tj-tq2hn6 (B2) owns the dev containment tests.
DEV_PROJECT_VERBS = frozenset(
    {
        'ps',
        'logs',
        'db_tables',
        'http_get',
        'down',
        'restart',
        'build',
        'up',
        'tools',
        'test_build',
        'test_up',
        # tj-iee6sn.10: the unified pair acts on the one project (DEV or TEST mode), not the agent stack.
        'migrate',
        'migrate_status',
    }
)
# The subset that runs PLAIN docker -- a label lookup, then `docker exec` -- rather than the dev
# compose prefix (ADR tj-4rr0la addendum 22). test_dev_exec.py owns their containment tests.
# migrate_status runs the mode guard's `docker ps` first, then the same lookup and exec.
DEV_EXEC_VERBS = frozenset({'db_tables', 'migrate_status', 'http_get'})
# The STATE-CHANGING label-only pair (tj-iee6sn.7): the D7 guard's plain `docker ps`, then ONE step
# under the four-word dev prefix. test_dev_label_verbs.py owns their order, argv and refusal tests.
DEV_LABEL_LIFECYCLE_VERBS = frozenset({'down', 'restart'})
# The FILE-BASED dev lifecycle verbs (tj-iee6sn.8 on): the trusted digest script under bash, the
# ensure-bases pair, then compose under the file-based dev prefix (stack.dev_file_prefix), which names
# the trusted dev files and the repository's host path. test_dev_build.py owns build's tests.
DEV_FILE_LIFECYCLE_VERBS = frozenset({'build'})
# up (tj-iee6sn.9) mixes all three: the D7 guard's `docker ps`, three `docker image inspect`s of the
# :dev images, the trusted digests, the label-only down, the devnet `docker network` pair, the bases,
# then the file-based up carried by the fixed `sh -c` secret wrapper. test_dev_up.py owns its tests.
DEV_UP_VERBS = frozenset({'up'})
# migrate (tj-iee6sn.10), STATE-CHANGING, file-based and not label-only: the mode guard's `docker ps`, the
# postgres and data_store lookups (`docker ps`), the mode's data_store image inspect, in TEST mode the
# data_ingest lookup, the two digests (bash) and a container stamp read per running service, the bases (the
# one-off `run` of data_store is flagged as building), then the one-off alembic run: in DEV carried by
# up's fixed `sh -c` secret wrapper, in TEST plain over base + fake. test_dev_migrate.py owns its tests.
DEV_MIGRATE_VERBS = frozenset({'migrate'})
# tools (tj-iee6sn.14), `make dev-tools`: the D7 guard's `docker ps`, the devnet `docker network` pair,
# then ONE file-based up of pgAdmin (the dev files plus docker-compose.tools.yaml, stack.dev_tools_prefix)
# carried by up's fixed `sh -c` secret wrapper. No image inspect, no digest, no base: pgadmin and
# postgres are image-only. test_dev_tools.py owns its tests.
DEV_TOOLS_VERBS = frozenset({'tools'})
# TEST MODE (tj-iee6sn.24; ADR tj-4rr0la addenda 26-27), over the same project. test_build: the trusted
# digests, the bases, then ONE file-based build over base + fake + the MCP-only test overlay. test_up: the
# mode guard's `docker ps`, the two :test image inspects, the digests, the two stamp reads, the bases, the
# label-only down, then ONE plain (unwrapped) file-based up over base + fake. test_test_mode.py owns both.
TEST_MODE_VERBS = frozenset({'test_build', 'test_up'})
# The wrapper's four constant leading words, spelled here from literals except the script, which is
# pinned whole in test_dev_up.py; the helper and the env file follow them.
DEV_UP_WRAPPER_LEAD = ('sh', '-c')


def is_dev_up_wrapper(step: stack.Step) -> bool:
    """The up step: `sh -c <script> sh <helper> <env file> <compose argv>`, the server's one shell."""
    return tuple(step.argv[:2]) == DEV_UP_WRAPPER_LEAD and len(step.argv) > 6 and step.argv[3] == 'sh'


def dev_up_compose_argv(step: stack.Step) -> tuple[str, ...]:
    """The compose argv the wrapper execs: every word after its six fixed ones."""
    assert is_dev_up_wrapper(step), step.argv
    return tuple(step.argv[6:])


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
