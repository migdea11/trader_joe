"""The MCP's constants, argument validation, the worktree snapshot and the command builders of the dev and test modes.

Nothing here runs Docker. The only subprocess in this module is one read-only git plumbing call,
through run_git(): `git worktree list --porcelain`. Every docker command is built here as an
argument list and run by runner.py.

THE DAEMON ONLY EVER SEES MCP-OWNED INPUTS (ADR tj-4rr0la addendum 5). The Docker daemon is root on
the host and resolves bind-mount sources and the build context there; BuildKit reads the Dockerfile.
A check on an agent-writable tree is always a race, so neither is ever pointed at one:
    THE SNAPSHOT. Before a verb builds or creates a container from a worktree, refresh_snapshot()
        copies the worktree's SNAPSHOT_SOURCES into <stack dir>/source, refusing any symlink or
        special file. The snapshot serves only run_system_tests and seed_dump: it is the project
        directory and the build context of test_client (testclient_prefix), and the checks on it
        (check_mount_sources, resolve_test_paths) are stable. Every other verb (the dev ones,
        test_build, test_up, migrate and migrate_status) reads no worktree and takes no snapshot: their compose files are the MCP image's
        baked copies and their project directory is the repository's host path.
    THE TRUSTED DOCKERFILE. The MCP-only overlays (docker-compose.agent-dev.yaml, -test.yaml and
        -client.yaml) set build.dockerfile to TRUSTED_DOCKERFILE, the
        MCP image's own copy of the repository's Dockerfile. The worktree's Dockerfile is never read,
        so FROM, COPY --from, RUN --mount from= and a syntax= frontend are fixed: a build cannot read
        prod images, the private repository's images or any other image on the host daemon. Its
        external refs are digest-pinned (BASE_IMAGES) and pulled by the daemon before every build,
        because buildx would otherwise fetch the registry token from inside agent_mcp, which has no
        egress (ADR tj-4rr0la addendum 13).
    ACCEPTED, RECORDED: RUN keeps bridge egress (uv sync downloads from PyPI) and runs agent-chosen
        code -- the packages the snapshot's pyproject.toml and uv.lock name, and their build hooks.
        What that code can read is the build container: the public base images and the snapshot,
        which is the agent's own content. Build output returns to the agent and can hold nothing it
        could not already read.
"""

import contextlib
import datetime
import os
import re
import shutil
import stat
import subprocess  # nosec B404 -- argument lists only, never a shell; see run_git
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from tools.agent_mcp.seeds import MAX_BUNDLE_BYTES


class Refused(Exception):
    """A verb refuses: raised before any docker subprocess runs."""


# Where the MCP image carries its OWN copies of the compose files (tools/agent_mcp/Dockerfile). Never the
# worktree's: a worktree is agent-writable, and compose content from it could mount any host path,
# add privileged: or the Docker socket -- raw compose pass-through, which ADR tj-4rr0la rejects (C).
# --project-directory points relative paths (build contexts, bind mounts) at the snapshot instead.
TRUSTED_COMPOSE_DIR = Path('/opt/agent_mcp/compose')
# The MCP image's own copy of the repository's Dockerfile, which the three MCP-only overlays
# (docker-compose.agent-dev.yaml, -test.yaml and -client.yaml) name as build.dockerfile for what they build
# (ADR tj-4rr0la addendum 5, ruling 2). A Dockerfile change reaches the MCP when its image is rebuilt, as a
# compose change does.
TRUSTED_DOCKERFILE = TRUSTED_COMPOSE_DIR / 'Dockerfile'
# Every external image TRUSTED_DOCKERFILE names (FROM and COPY --from; stage names are not external),
# exactly as it spells them: tag@sha256:<multi-arch index digest>. Before any build the MCP has the
# DAEMON make each one present -- `docker image inspect`, then `docker pull` only if absent -- so the
# build never resolves a registry from inside agent_mcp, which has no egress (ADR tj-4rr0la addenda
# 13-14). This module reaches the MCP image by the same build, from the same checkout, as the trusted
# Dockerfile (tools/agent_mcp/Dockerfile copies tools/agent_mcp/*.py and the root Dockerfile), so the
# two always agree within one image. This constant is the ONLY place the list lives: nothing parses it
# from the snapshot or a worktree. A digest bump in the root Dockerfile updates it in the same change;
# a validator test pins the two equal (tj-c4mosr.14).
BASE_IMAGES = (
    'debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251',
    'ghcr.io/astral-sh/uv:0.12.19@sha256:04d046b13e60d6bcec73cbc5e1cad25d680dea90c8573340950a0ac2d1aef424',
    'caddy:2.11.7-alpine@sha256:d8542f48d34a9cf4e4c11a478865229840e87e4c96ea3f439101f31a5d35f75f',
)

DOCKER = '/usr/local/bin/docker'
GIT = '/usr/bin/git'
# Every docker subprocess reaches the daemon through the socket proxy, never a socket (ADR tj-4rr0la
# addendum 1 (b)).
DOCKER_HOST = 'tcp://socket_proxy:2375'

# Test mode's built services (data_store and data_ingest) and the system-test client; builds() reads it: a
# `run` of one of them builds its image when it is missing.
BUILT_SERVICES = ('data_store', 'data_ingest', 'test_client')
TAIL_DEFAULT = 200
TAIL_MAX = 2000
MAX_TEST_PATHS = 50

# ---------------------------------------------------------------------------------------------
# THE USER'S OWN DEV COMPOSE PROJECT -- READ ONLY, TWO VERBS (tj-kzy7w2; user ruling on tj-wpnr7o (b),
# recorded on tj-izzqub addendum 1; ADR tj-4rr0la addendum 18). This is the ONE place in the server
# that names a compose project, DEV_PROJECT, and everything below exists to keep it that way.
#
# WHY IT IS HERE. An agent already reaches the user's dev services by name over devnet (ADR tj-q9ae5u
# item 3) but cannot see WHY one is unhealthy; logs and container state were the one diagnostic still
# fetched by hand. The user ruled "read-only only" and ruled OUT a Docker socket, which is host root.
#
# WHAT MAKES THESE SAFE, and it is structural rather than conventional: the dev verbs run
# `docker compose -p <project>` and NOTHING ELSE -- no -f, no --project-directory, no --env-file.
# compose v2 does not load a project at all when a project NAME is given and no file is
# (cmd/compose/compose.go, projectOrName: it builds a project only when ConfigPaths is non-empty or
# the name is empty), and `ps` and `logs` then work off the com.docker.compose.project LABEL on the
# running containers. With no compose file there is no build context, no bind mount, no service
# definition and no interpolation, so `up`, `run`, `build` and `exec` are not merely unused here --
# they have nothing to act on. The user's root .env, whose read guard this epic must not defeat, is
# never opened: compose reads a project directory's env file only while loading a project.
# check_dev_argv() below re-checks the words actually handed to the daemon, so the containment is a
# property of the argv rather than of the builders that produce it.
#
# THE PROJECT NAME IS A CONSTANT, never an argument and never derived from anything an agent writes.
# Dev and test run as the explicit compose project trader_joe_dev (Makefile -p; ADR tj-4rr0la
# addendum 29, decision tj-oxxl8a option A). PROD keeps compose's default project (the checkout
# directory's name) and no MCP command ever names it: no argv, lookup filter, pinned
# COMPOSE_PROJECT_NAME or text here uses the bare name. A prod stack therefore sits in a different
# project from everything these verbs reach, and the mode guard below stays as defence in depth.
DEV_PROJECT = 'trader_joe_dev'
# The long-running services of the dev set (docker-compose.yaml, the override and the web files).
# pgAdmin lives in docker-compose.tools.yaml, a file no log or ps verb loads, and is deliberately absent.
DEV_SERVICES = ('postgres', 'data_store', 'data_ingest', 'web')
# THE FILE-BASED DEV SET (ADR tj-4rr0la addendum 23; tj-iee6sn.5), for the verbs that build or create
# containers. The Makefile's DEV_COMPOSE -f list, in its order, then the MCP-only overlay LAST so its
# build.dockerfile wins. These are names; dev_file_prefix() resolves each one in TRUSTED_COMPOSE_DIR, the
# copies baked into the MCP image, never the checkout's own. A validator test pins the first four equal
# to DEV_COMPOSE, so a file added to or reordered in the Makefile turns it red.
DEV_COMPOSE_FILES = (
    'docker-compose.yaml',
    'docker-compose.override.yaml',
    'docker-compose.web.yaml',
    'docker-compose.web.dev.yaml',
    'docker-compose.agent-dev.yaml',
)
# tools' set: the Makefile's TOOLS_COMPOSE (the first four plus the tools file, pgAdmin and nothing
# else), then the MCP-only overlay last.
DEV_TOOLS_COMPOSE_FILE = 'docker-compose.tools.yaml'
DEV_TOOLS_COMPOSE_FILES = (*DEV_COMPOSE_FILES[:-1], DEV_TOOLS_COMPOSE_FILE, DEV_COMPOSE_FILES[-1])
# TEST MODE's sets (ADR tj-4rr0la addendum 27; tj-iee6sn.24). TEST_COMPOSE_FILES is the Makefile's
# SYSTEM_COMPOSE list, base then the fake-mode overlay (which names the :test image tags), for test_up.
# TEST_BUILD_COMPOSE_FILES adds the MCP-only overlay LAST, which sets build.dockerfile for data_store and
# data_ingest alone, for test_build. Neither set holds the override, the web files or the tools file.
TEST_COMPOSE_FILES = ('docker-compose.yaml', 'docker-compose.fake.yaml')
TEST_OVERLAY_FILE = 'docker-compose.agent-test.yaml'
TEST_BUILD_COMPOSE_FILES = (*TEST_COMPOSE_FILES, TEST_OVERLAY_FILE)
# THE SYSTEM-TEST CLIENT's set (run_system_tests, seed_dump; tj-iee6sn.20): the Makefile's
# TEST_CLIENT_COMPOSE (the base file and the client file), then the MCP-only overlay LAST, which sets
# test_client's build.dockerfile alone. It holds no fake file: only test_client is run from it, against the
# services test_up started.
SYSTEM_CLIENT_FILE = 'docker-compose.test-client.yaml'
SYSTEM_CLIENT_OVERLAY_FILE = 'docker-compose.agent-client.yaml'
SYSTEM_CLIENT_COMPOSE_FILES = (TEST_COMPOSE_FILES[0], SYSTEM_CLIENT_FILE, SYSTEM_CLIENT_OVERLAY_FILE)
# The Makefile's TEST_DATA_DIR. Pinned in the step env of test_up and test_build ONLY (process env
# outranks the project's .env), never of a dev verb: DEV keeps the owner's own DATA_DIR.
TEST_DATA_DIR = './volume_data/test'
# The trusted copies of the two scripts the dev verbs run, in a tools/ directory beside the trusted
# Dockerfile (tools/agent_mcp/Dockerfile): dev_instance_secret.sh resolves the dev web container's
# INSTANCE_WRITE_SECRET, source_digest.sh computes the source stamp.
TRUSTED_DEV_INSTANCE_SECRET = TRUSTED_COMPOSE_DIR / 'tools' / 'dev_instance_secret.sh'
TRUSTED_SOURCE_DIGEST = TRUSTED_COMPOSE_DIR / 'tools' / 'source_digest.sh'
# The only compose subcommands a dev verb may spell. Both are read-only: `ps` lists the project's
# containers, `logs` prints what they already wrote. Nothing is added here without the ADR addendum
# that allowing it would need.
DEV_READ_COMMANDS = frozenset({'ps', 'logs'})
# Options that would point a dev verb at content, a different project or an env file. Refused
# wherever they appear after the prefix -- `-p` included, so the one in the prefix stays the only one.
DEV_FORBIDDEN_OPTIONS = frozenset(
    {'-f', '--file', '-p', '--project-name', '--project-directory', '--env-file', '--profile'}
)
# The ATTACHED form of a forbidden SHORT option (tj-v4e9ke). docker's CLI reads `-fFILE` and `-pNAME`
# exactly as the separated pair, and the scan's `=` split does not see them: only the attached LONG
# form spells its value after an `=`. Derived from the set above rather than spelled a second time,
# so a short option added there is refused in both forms. Short only: a long option's second
# character is '-', so '--profile' does not start with '-p' and '--file' does not start with '-f'.
# These are matched against the TAIL alone -- the prefix's own legitimate `-p DEV_PROJECT` sits in
# the head that check_dev_argv compares against dev_compose_prefix(), ahead of the scan.
DEV_FORBIDDEN_SHORT_PREFIXES = tuple(
    sorted(option for option in DEV_FORBIDDEN_OPTIONS if len(option) == 2 and not option.startswith('--'))
)

# THE SNAPSHOT (ADR tj-4rr0la addendum 5, ruling 1): what refresh_snapshot() copies from a worktree
# into <stack dir>/SOURCE_DIR_NAME, the compose project directory of every verb. EXACTLY the trusted
# Dockerfile's COPY sources from the build context (pyproject.toml, uv.lock, entrypoint.sh, common,
# routers, schemas, gen/proto/python -- the generated gRPC code, decision tj-3mk3u5.42 F1; generated
# by `make proto` into the worktree and never committed, so a worktree that has not built or tested
# yet has nothing to copy here --
# and ${SERVICE_PATH}/${SERVICE_NAME}/app for data_store and data_ingest) plus the trusted compose
# files' relative bind sources (common, gen/proto/python, routers, schemas, data/store/app,
# data/store/alembic.ini, data/store/migrations, tests/system, pytest.ini; tests/fakes, the
# fake-mode overlay's read-only mount, tj-vhboky.61; and test_client's read-only mounts of the seed
# producer's import closure, data/store/seeds, data/ingest/app and tests/fakes, ADR tj-4rr0la
# addendum 10 (2)). A validator test pins the equality by parsing those files (tj-c4mosr.5 S2), so a
# new COPY or mount without an entry goes red. An allow-list, never the whole tree: the root
# checkout's live env files, .venv, data directories and .git are never copied, whatever the
# (agent-writable) .dockerignore says. The build context '.' is the snapshot root itself.
SOURCE_DIR_NAME = 'source'
#
# WORKTREE-RELATIVE PATHS, so the four service trees carry the server/ prefix they gained in epic
# tj-iontkq.4 and the root siblings -- gen/, tests/, pyproject.toml, uv.lock, entrypoint.sh,
# pytest.ini -- do not. The snapshot reproduces the worktree's own layout, so what compose and the
# trusted Dockerfile resolve against it keeps working unchanged.
SNAPSHOT_SOURCES = (
    'pyproject.toml',
    'uv.lock',
    'entrypoint.sh',
    'server/common',
    'server/routers',
    'server/schemas',
    'gen/proto/python',
    'server/data/store/app',
    'server/data/ingest/app',
    'server/data/store/alembic.ini',
    'server/data/store/migrations',
    'server/data/store/seeds',
    'tests/system',
    'tests/fakes',
    'pytest.ini',
)
# The copy's caps: generous (the sources are a few MB today), there to stop a worktree that grew a
# dataset or a runaway generator from filling the host disk.
SNAPSHOT_MAX_BYTES = 200 * 1024 * 1024
SNAPSHOT_MAX_FILES = 20_000
SYSTEM_TESTS_DIR = 'tests/system'
# Worktree- and snapshot-relative, like SNAPSHOT_SOURCES above: data/store travels with the
# service trees under server/ (tj-iontkq.4), while tests/system stays at the top.
MIGRATION_VERSIONS_DIR = 'server/data/store/migrations/versions'

# ---------------------------------------------------------------------------------------------
# THE STACK DIRECTORY (settings.stack_dir; ADR tj-4rr0la addenda 1 (d) and 5): the snapshot and the audit log.
AUDIT_LOG_NAME = 'audit.log'

_WORKTREE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$')
# seed_dump's --date: ASCII digits only (\d would take any Unicode digit), then a real calendar date.
_SEED_DATE = re.compile(r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$')


# ---------------------------------------------------------------------------------------------
# GIT. An agent can write this repository's .git/config, and a config key like core.fsmonitor makes
# git EXECUTE a command -- inside the one container that holds Docker access. So git runs plumbing
# only, with the executing hooks switched off, no system config, no pager and a fixed environment.
_GIT_ENV = {
    'PATH': '/usr/bin:/bin',
    'GIT_CONFIG_NOSYSTEM': '1',
    'GIT_TERMINAL_PROMPT': '0',
    'GIT_PAGER': 'cat',
    'LC_ALL': 'C',
}
_GIT_HARDENING = ('--no-pager', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null')


def run_git(repo_root: Path, args: Sequence[str]) -> str:
    """Run one read-only git plumbing command in the main checkout and return its stdout."""
    try:
        completed = subprocess.run(  # nosec B603 -- fixed argument list, no shell
            [GIT, *_GIT_HARDENING, *args],
            cwd=repo_root,
            env=_GIT_ENV,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Refused(f'git could not run: {type(error).__name__}') from error
    if completed.returncode != 0:
        raise Refused(f'git {args[0]} failed: {completed.stderr.strip()[:500]}')
    return completed.stdout


def parse_worktree_porcelain(text: str) -> list[tuple[Path, bool]]:
    """Parse `git worktree list --porcelain` into (path, usable) pairs, the main worktree first.

    A bare or prunable entry (its directory is gone) is listed as not usable.
    """
    entries: list[tuple[Path, bool]] = []
    for block in text.strip().split('\n\n'):
        lines = block.splitlines()
        if not lines or not lines[0].startswith('worktree '):
            continue
        path = Path(lines[0].removeprefix('worktree '))
        usable = not any(line == 'bare' or line.startswith('prunable') for line in lines[1:])
        entries.append((path, usable))
    return entries


def _within(path: Path, base: Path) -> bool:
    return path == base or base in path.parents


def list_worktrees(repo_root: Path) -> dict[str, Path]:
    """Every worktree of this repository by name: 'root' for the main checkout, else the basename.

    Only worktrees that lie under the repository root are listed (the only ones the MCP container
    can see; the harness puts agents' under .claude/worktrees). Two worktrees with one basename are
    both left out rather than guessed between.
    """
    entries = parse_worktree_porcelain(run_git(repo_root, ['worktree', 'list', '--porcelain']))
    if not entries:
        raise Refused('git worktree list returned nothing')
    named: dict[str, Path] = {}
    ambiguous: set[str] = set()
    for index, (path, usable) in enumerate(entries):
        resolved = Path(os.path.realpath(path))
        if not usable or not _within(resolved, repo_root) or not resolved.is_dir():
            continue
        name = 'root' if index == 0 else path.name
        if name in named:
            ambiguous.add(name)
        named[name] = resolved
    for name in ambiguous:
        del named[name]
    return named


# ---------------------------------------------------------------------------------------------
# ARGUMENT VALIDATION. Each raises Refused naming what was wrong, before any docker subprocess.
def check_worktree_name(name: object) -> str:
    """The worktree NAME's shape: 'root' or a plain name. Runs before any git call (ADR s5(a))."""
    if not isinstance(name, str) or not _WORKTREE_NAME.match(name):
        raise Refused("worktree must be 'root' or the name of one of this repository's worktrees")
    return name


def resolve_worktree(name: object, worktrees: Mapping[str, Path]) -> Path:
    """Resolve a worktree NAME against `git worktree list` of this repository."""
    checked = check_worktree_name(name)
    if checked not in worktrees:
        raise Refused(f'no worktree named {checked!r}; known: {", ".join(sorted(worktrees))}')
    return worktrees[checked]


def check_mount_sources(snapshot: Path) -> None:
    """Refuse unless every SNAPSHOT_SOURCES entry is present in the SNAPSHOT, not a symlink, inside it.

    Runs on the snapshot after refresh_snapshot(), never on a worktree: nothing but the MCP writes
    under the stack directory, so what this checks is what the daemon later mounts and builds from.
    """
    for source in SNAPSHOT_SOURCES:
        candidate = snapshot / source
        if candidate.is_symlink():
            raise Refused(f'{source} in the snapshot is a symlink; refusing to mount it')
        resolved = Path(os.path.realpath(candidate))
        if not _within(resolved, snapshot):
            raise Refused(f'{source} in the snapshot resolves outside it; refusing to mount it')
        if not resolved.exists():
            raise Refused(f'{source} is missing from the snapshot')


def check_test_paths(paths: object) -> list[str]:
    """run_system_tests' paths, checked by their spelling alone: before any subprocess or copy.

    Each is relative, no option, and normalises to tests/system or below it. Returns the normalised
    spellings; an empty list means the whole suite. resolve_test_paths() then checks them against
    the snapshot.
    """
    if not isinstance(paths, list) or len(paths) > MAX_TEST_PATHS:
        raise Refused(f'paths must be a list of at most {MAX_TEST_PATHS} paths under {SYSTEM_TESTS_DIR}')
    normalised = []
    for raw in paths:
        if not isinstance(raw, str) or not raw or '\0' in raw or raw.startswith('-') or os.path.isabs(raw):
            raise Refused(f'each path must be relative to the worktree and lie under {SYSTEM_TESTS_DIR}')
        spelled = os.path.normpath(raw)
        if spelled != SYSTEM_TESTS_DIR and not spelled.startswith(f'{SYSTEM_TESTS_DIR}/'):
            raise Refused(f'{raw!r} does not lie under {SYSTEM_TESTS_DIR}')
        normalised.append(spelled)
    return normalised


def resolve_test_paths(snapshot: Path, paths: object) -> list[str]:
    """Validate run_system_tests' paths against the SNAPSHOT: each must resolve under tests/system.

    Returns them relative to the snapshot, resolved, which is where test_client mounts them
    (./tests/system -> /code/tests/system, working_dir /code). An empty list means the whole suite.
    Paths only, no pytest node ids and no options.
    """
    checked = check_test_paths(paths)
    base = Path(os.path.realpath(snapshot / SYSTEM_TESTS_DIR))
    if not _within(base, snapshot) or not base.is_dir():
        raise Refused(f'{SYSTEM_TESTS_DIR} is missing from the snapshot, or resolves outside it')
    if not checked:
        return [SYSTEM_TESTS_DIR]
    resolved_paths = []
    for spelled in checked:
        resolved = Path(os.path.realpath(snapshot / spelled))
        if not _within(resolved, base) or not resolved.exists():
            raise Refused(f'{spelled!r} does not resolve to an existing path under {SYSTEM_TESTS_DIR}')
        resolved_paths.append(resolved.relative_to(snapshot).as_posix())
    return resolved_paths


def check_seed_date(value: object) -> str | None:
    """seed_dump's optional date: None, or YYYY-MM-DD in ASCII digits that is a real calendar date.

    Checked before any git, copy or docker call, and the refusal never echoes the value.
    """
    if value is None:
        return None
    if not isinstance(value, str) or not _SEED_DATE.fullmatch(value):
        raise Refused('date must be YYYY-MM-DD')
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        raise Refused('date must be a real calendar date, YYYY-MM-DD') from None
    return value


def clamp_tail(tail: object) -> int:
    """An integer, clamped to 1..TAIL_MAX."""
    if isinstance(tail, bool) or not isinstance(tail, int):
        raise Refused('tail must be an integer')
    return max(1, min(tail, TAIL_MAX))


def validate_dev_service(service: object) -> str:
    """Service must be one of the closed set DEV_SERVICES. Separate from validate_service by design.

    The two enums happen to hold the same names today, because both projects are built from
    docker-compose.yaml. They are kept apart so that adding a service to one stack cannot silently
    widen what the other's verb accepts (tj-kzy7w2: keep the two projects apart in the code).
    """
    if not isinstance(service, str) or service not in DEV_SERVICES:
        raise Refused(f'service must be one of: {", ".join(DEV_SERVICES)}')
    return service


# ---------------------------------------------------------------------------------------------
# THE SNAPSHOT (ADR tj-4rr0la addendum 5, ruling 1). PREMISE, named: nothing but the MCP writes under
# the stack directory. settings.load_settings refuses a stack directory inside or containing the
# repository or AGENT_HOME_PATH (the devcontainer mounts both), and test_client gets no writable bind
# into the snapshot (its mounts are read-only).
#
# One snapshot operation at a time, across threads: a verb that timed out leaves its copy thread
# running until it next checks its cancel event, and the next verb's copy must not interleave with it.
_SNAPSHOT_LOCK = threading.Lock()
_O_NOFOLLOW_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
_O_NOFOLLOW_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_COPY_CHUNK = 1024 * 1024


def _make_snapshot_dir(path: Path, *, parents: bool = False) -> None:
    """Create a snapshot directory 0755, whatever the umask.

    World-readable on purpose: test_client reads the snapshot through bind mounts
    as its own user (appuser, not the MCP's), and BuildKit sends it as the build context. It holds
    only the worktree's own source, which the agent can already read; live env files are never
    copied (_skipped_file).

    Follows no link at PATH itself: os.mkdir (a missing parent created the same way, one level at a
    time, when PARENTS), then the directory is opened O_DIRECTORY|O_NOFOLLOW and the mode set on that
    descriptor. An existing entry at PATH that is a symlink or not a directory is Refused, so the
    helper does not rely on its callers having cleared the path (ADR tj-4rr0la addendum 6, L2).
    """
    if parents and not os.path.lexists(path.parent):
        _make_snapshot_dir(path.parent, parents=True)
    with contextlib.suppress(FileExistsError):
        os.mkdir(path, 0o755)
    try:
        fd = os.open(path, _O_NOFOLLOW_DIR)
    except OSError as error:
        raise Refused(f'snapshot directory {path.name} is a symlink or not a directory: {error.strerror}') from error
    try:
        # A DIRECTORY needs the x bits to be entered; the rule's suggested 0o644 would lock the
        # containers out. Reviewed exception, scoped to this one line (ADR tj-4rr0la addendum 6).
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.fchmod(fd, 0o755)  # nosec B103 -- source-only tree the stack's containers must read; see docstring
    finally:
        os.close(fd)


def _skipped_file(name: str) -> bool:
    """A live env file (.env, .env.*) is never copied; the committed .env.default is."""
    return (name == '.env' or name.startswith('.env.')) and name != '.env.default'


@dataclass
class _Copy:
    """One snapshot copy in progress: the destination root, the running totals and the cancel event."""

    destination: Path
    cancel: threading.Event
    files: int = 0
    total_bytes: int = 0

    def tick(self, relative: str) -> None:
        if self.cancel.is_set():
            raise Refused('the snapshot was cancelled (the verb timed out)')
        self.files += 1
        if self.files > SNAPSHOT_MAX_FILES:
            raise Refused(f'the snapshot passed its cap of {SNAPSHOT_MAX_FILES} files at {relative}')

    def add_bytes(self, count: int, relative: str) -> None:
        self.total_bytes += count
        if self.total_bytes > SNAPSHOT_MAX_BYTES:
            raise Refused(f'the snapshot passed its cap of {SNAPSHOT_MAX_BYTES} bytes at {relative}')

    def make_dir(self, relative: str) -> None:
        _make_snapshot_dir(self.destination / relative, parents=True)

    def copy_file(self, dir_fd: int, name: str, relative: str) -> None:
        """Copy one regular file, opened without following a symlink, relative to its directory."""
        self.tick(relative)
        try:
            source_fd = os.open(name, _O_NOFOLLOW_FILE, dir_fd=dir_fd)
        except OSError as error:
            raise Refused(f'{relative} could not be opened without following a link: {error.strerror}') from error
        try:
            opened = os.fstat(source_fd)
            if not stat.S_ISREG(opened.st_mode):
                raise Refused(f'{relative} is not a regular file; refusing to copy it')
            mode = 0o755 if opened.st_mode & 0o111 else 0o644
            target_fd = os.open(self.destination / relative, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
            try:
                os.fchmod(target_fd, mode)
                while chunk := os.read(source_fd, _COPY_CHUNK):
                    self.add_bytes(len(chunk), relative)
                    os.write(target_fd, chunk)
            finally:
                os.close(target_fd)
        finally:
            os.close(source_fd)

    def copy_tree(self, parent_fd: int, name: str, relative: str) -> None:
        """Copy a directory with os.fwalk, never following a symlink; refuse any link or special file.

        os.fwalk sorts a symlink to a directory into dirnames (its scandir is_dir() follows links)
        and then declines to enter it, silently -- so every dirname is lstat'd here, and a link
        among them refused, rather than left to fwalk to skip.
        """

        def refuse(error: OSError) -> None:
            raise Refused(f'{relative} could not be read: {error.strerror}') from error

        try:
            for dirpath, dirnames, filenames, dir_fd in os.fwalk(
                name, dir_fd=parent_fd, follow_symlinks=False, onerror=refuse
            ):
                here = relative + dirpath.removeprefix(name)
                self.make_dir(here)
                dirnames[:] = [entry for entry in dirnames if entry != '__pycache__']
                for entry in dirnames + filenames:
                    entry_relative = f'{here}/{entry}'
                    found = os.stat(entry, dir_fd=dir_fd, follow_symlinks=False)
                    if stat.S_ISLNK(found.st_mode):
                        raise Refused(f'{entry_relative} is a symlink; the snapshot copies no links')
                    if stat.S_ISDIR(found.st_mode) and entry in dirnames:
                        self.tick(entry_relative)
                        continue
                    if not stat.S_ISREG(found.st_mode):
                        raise Refused(f'{entry_relative} is not a regular file or directory; refusing to copy it')
                    if not _skipped_file(entry):
                        self.copy_file(dir_fd, entry, entry_relative)
        except OSError as error:
            raise Refused(f'{relative} could not be read: {error.strerror}') from error


def _open_parents(root_fd: int, parts: Sequence[str], source: str) -> int:
    """Open each directory above a source, relative to the one before, never following a symlink."""
    fd = os.dup(root_fd)
    for part in parts:
        try:
            next_fd = os.open(part, _O_NOFOLLOW_DIR, dir_fd=fd)
        except OSError as error:
            os.close(fd)
            raise Refused(f'{source}: {part} is missing, a symlink or not a directory') from error
        os.close(fd)
        fd = next_fd
    return fd


def _copy_source(copy: _Copy, root_fd: int, source: str) -> None:
    *parents, name = source.split('/')
    parent_fd = _open_parents(root_fd, parents, source)
    try:
        try:
            found = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError as error:
            raise Refused(f'{source} is missing from the worktree') from error
        if stat.S_ISLNK(found.st_mode):
            raise Refused(f'{source} is a symlink; the snapshot copies no links')
        if parents:
            copy.make_dir('/'.join(parents))
        if stat.S_ISDIR(found.st_mode):
            copy.copy_tree(parent_fd, name, source)
        elif stat.S_ISREG(found.st_mode):
            copy.copy_file(parent_fd, name, source)
        else:
            raise Refused(f'{source} is not a regular file or directory; refusing to copy it')
    finally:
        os.close(parent_fd)


def verify_snapshot(snapshot: Path) -> None:
    """Refuse unless the copy's root resolves to itself and holds no symlink or special file anywhere."""
    if Path(os.path.realpath(snapshot)) != snapshot:
        raise Refused('the snapshot does not resolve to its configured path')
    for dirpath, dirnames, filenames in os.walk(snapshot, followlinks=False):
        for entry in dirnames + filenames:
            mode = os.lstat(os.path.join(dirpath, entry)).st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                relative = os.path.relpath(os.path.join(dirpath, entry), snapshot)
                raise Refused(f'the snapshot holds {relative}, which is not a regular file or directory')
    for source in SNAPSHOT_SOURCES:
        if not os.path.lexists(snapshot / source):
            raise Refused(f'{source} is missing from the snapshot')


def _remove_generation(path: Path) -> None:
    """Remove an MCP-owned snapshot generation, symlink-safe; a symlink in its place is unlinked."""
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    if not path.exists():
        return
    if not shutil.rmtree.avoids_symlink_attacks:
        raise Refused('this platform cannot delete a tree without following symlinks')
    shutil.rmtree(path)


def snapshot_dir(stack_dir: Path) -> Path:
    """The snapshot's fixed path: the compose project directory of every verb."""
    return stack_dir / SOURCE_DIR_NAME


def _build_generation(new: Path, worktree: Path, cancel: threading.Event) -> None:
    """Copy the worktree's SNAPSHOT_SOURCES into NEW and verify it; refresh_snapshot holds the lock."""
    _make_snapshot_dir(new)
    copy = _Copy(destination=new, cancel=cancel)
    try:
        root_fd = os.open(worktree, _O_NOFOLLOW_DIR)
    except OSError as error:
        raise Refused('the worktree could not be opened without following a link') from error
    try:
        for source in SNAPSHOT_SOURCES:
            _copy_source(copy, root_fd, source)
    finally:
        os.close(root_fd)
    verify_snapshot(new)
    if cancel.is_set():
        raise Refused('the snapshot was cancelled (the verb timed out)')


def refresh_snapshot(stack_dir: Path, worktree: Path, cancel: threading.Event | None = None) -> Path:
    """Copy the worktree's SNAPSHOT_SOURCES into <stack_dir>/source, replacing the old copy. Returns it.

    The copy is built beside the live one in source.new (a leftover cleared first), from a walk that
    never follows a symlink: directories opened O_NOFOLLOW relative to their parent's descriptor,
    os.fwalk(follow_symlinks=False), files opened O_RDONLY|O_NOFOLLOW|O_NONBLOCK and fstat'd after
    the open. It refuses (Refused, naming the relative path) on a symlink anywhere, a FIFO, socket or
    device, a missing entry, or more than SNAPSHOT_MAX_BYTES / SNAPSHOT_MAX_FILES. It skips
    __pycache__ and every .env / .env.* file but .env.default. Files are written 0644 (0755 when any
    exec bit was set), directories 0755. The copy is then verified (verify_snapshot), and swapped in
    by rename: source -> source.old, source.new -> source; source.old is removed. A copy that is
    refused, cancelled or fails before the swap removes source.new and leaves source as it was.

    The snapshot is of the WORKING TREE, so uncommitted and untracked work is carried over. The MCP
    process's own reads are bounded by its container's mounts, all of which the agent can already
    read; the no-follow walk is defence in depth, the snapshot is the control.

    PREMISE: nothing but the MCP writes under the stack directory (see _SNAPSHOT_LOCK's comment).
    Blocking; runner.py calls it through asyncio.to_thread, with CANCEL set on the verb's timeout.
    """
    cancel = cancel or threading.Event()
    live = snapshot_dir(stack_dir)
    new = stack_dir / f'{SOURCE_DIR_NAME}.new'
    old = stack_dir / f'{SOURCE_DIR_NAME}.old'
    with _SNAPSHOT_LOCK:
        _remove_generation(new)
        _remove_generation(old)
        try:
            _build_generation(new, worktree, cancel)
        except BaseException:
            # A refused, cancelled or failed copy must not leave up to SNAPSHOT_MAX_BYTES in source.new
            # until the next refresh (ADR tj-4rr0la addendum 6, L1). The live snapshot is untouched. A
            # failure to clean up must not hide the reason the copy failed.
            with contextlib.suppress(OSError, Refused):
                _remove_generation(new)
            raise
        if live.is_symlink() or live.exists():
            os.rename(live, old)
        os.rename(new, live)
        _remove_generation(old)
        if Path(os.path.realpath(live)) != live:
            raise Refused('the snapshot does not resolve to its configured path')
    return live


# ---------------------------------------------------------------------------------------------
# COMMANDS. Every docker command is built here as an argument list: the dev and test-mode builders start
# from dev_compose_prefix() (project DEV_PROJECT, the trusted -f files, the repository's host path as the
# project directory), the system-test client from testclient_prefix() (the snapshot as the project
# directory). The builders take the stack directory, never a worktree: no compose argv carries a
# repository path an agent chose.
@dataclass(frozen=True)
class Step:
    """One subprocess: its argument list and the directory it runs in (the snapshot).

    builds is True for a step that CAN build an image from TRUSTED_DOCKERFILE -- compose builds a
    missing image on up/run without --build (ADR tj-4rr0la addendum 15). the step builders set it from the
    compose command (builds()), so no builder can add a build without it; the runner makes BASE_IMAGES
    present before the first such step of a verb and never runs it when a base could not be pulled.

    stdout_cap, when set, marks the step's stdout as DATA rather than output (seed_dump's bundle):
    the runner reads it in full up to stdout_cap bytes plus one -- not under the OUTPUT_CAP_BYTES
    tail rule -- and withholds it from the response and the audit line.

    env, when set, is merged over the runner's fixed environment for THIS step only (the dev
    lifecycle's pinned variables, dev_pinned_step_env). It is never rendered into the recorded
    command and never audited. Pairs, not a dict, so a Step stays hashable.
    """

    argv: tuple[str, ...]
    cwd: Path
    builds: bool = False
    stdout_cap: int | None = None
    env: tuple[tuple[str, str], ...] = ()


def builds(tail: Sequence[str]) -> bool:
    """Whether a compose command (the words after the project prefix) CAN build.

    compose builds a missing image on up/run without --build, so this is: `build`; any `--build`;
    `up`, always (an `up` naming no service builds every one); or a `run` naming a BUILT_SERVICES
    service (postgres is image-only, so a run of it stays base-free). A word
    that matches by coincidence costs an extra inspect, the safe direction.
    """
    if not tail:
        return False
    first = tail[0]
    return (
        first in ('build', 'up')
        or '--build' in tail
        or (first == 'run' and any(word in BUILT_SERVICES for word in tail))
    )


# THE BASES. Plain docker commands, not compose: through the same socket proxy (DOCKER_HOST in the
# runner's fixed environment), where IMAGES and POST already allow both. `docker pull` is POST
# /images/create, so the daemon fetches the token and the layers with the host's network; nothing
# here adds a network to agent_mcp, and no build passes --pull.
def base_inspect_step(ref: str, cwd: Path) -> Step:
    """`docker image inspect` of one BASE_IMAGES ref: exit status 0 when the daemon already has it."""
    return Step((DOCKER, 'image', 'inspect', '--format', '{{.Id}}', ref), cwd)


def base_pull_step(ref: str, cwd: Path) -> Step:
    """`docker pull` of one BASE_IMAGES ref, run only after its inspect found it absent."""
    return Step((DOCKER, 'pull', ref), cwd)


# THE SYSTEM-TEST CLIENT (run_system_tests, seed_dump; tj-iee6sn.20; ADR tj-4rr0la addenda 26-27). Both
# verbs run test_client of the ONE project, in TEST mode: the services are the :test images test_up
# started from the root checkout, and the client and every mount it has come from the SNAPSHOT of the named
# worktree, so a worktree's unintegrated tests and code reach the client. The command is
#   docker compose -p trader_joe_dev --project-directory <snapshot> --env-file <repo host path>/.env
#   -f <trusted base> -f <trusted client file> -f <trusted client overlay> <tail>
# No secret wrapper: test_up starts data_store unwrapped, so its INSTANCE_WRITE_SECRET is the root env
# file's value as compose interpolates it, and the client interpolates the same file the same way -- the
# two always agree, and a blank value fails conftest's clear missing-variable check instead of a 401. The
# secret is on no command line. The --env-file is a fixed word of THIS prefix: every other check refuses
# it, and check_testclient_argv accepts it here and nowhere else. The step env pins the redirecting
# variables and points the three service env-file variables at /dev/null (the client reads no env file,
# and the snapshot holds none); it carries no DATA_DIR and no placeholder, so the client never sees the
# test data directory and the real credentials come from --env-file alone.
SYSTEM_CLIENT_BUILD_TAIL = ('build', 'test_client')
SYSTEM_TESTS_RUN_HEAD = ('run', '--rm', '--no-deps', '--build', 'test_client')


def testclient_prefix(stack_dir: Path, repo_host_path: Path) -> list[str]:
    """`docker compose -p DEV_PROJECT --project-directory <snapshot> --env-file <repo host path>/.env` and one trusted -f per client file."""
    words = [
        DOCKER,
        'compose',
        '-p',
        DEV_PROJECT,
        '--project-directory',
        str(snapshot_dir(stack_dir)),
        '--env-file',
        str(repo_host_path / DEV_ENV_FILES[0]),
    ]
    for name in SYSTEM_CLIENT_COMPOSE_FILES:
        words += ['-f', str(TRUSTED_COMPOSE_DIR / name)]
    return words


def testclient_step_env() -> tuple[tuple[str, str], ...]:
    """The client steps' environment: the steering variables pinned and the three service env files at /dev/null. No DATA_DIR, no credential."""
    return (*((name, '/dev/null') for name, _ in _DEV_ENV_FILE_VARIABLES), *_DEV_STEERING_ENV)


def _check_suite_path(path: str) -> None:
    """Refuse a run_system_tests path that is not a normalised relative path at or under tests/system."""
    if (
        not path
        or '\0' in path
        or path.startswith('-')
        or os.path.isabs(path)
        or os.path.normpath(path) != path
        or (path != SYSTEM_TESTS_DIR and not path.startswith(f'{SYSTEM_TESTS_DIR}/'))
    ):
        raise Refused(f'a client path is a normalised path at or under {SYSTEM_TESTS_DIR}, and nothing else')


def check_testclient_argv(argv: Sequence[str], stack_dir: Path, repo_host_path: Path) -> None:
    """Refuse any argv but the client prefix and then exactly one of the three client tails; the secret wrapper is refused.

    The tails: `build test_client`; `run --rm --no-deps --build test_client <paths under tests/system>`; the
    seed producer's one invocation with an optional `--date D` (a real ASCII calendar date). Checked on the
    words that go to the daemon, not on the builders' intent.
    """
    words = tuple(argv)
    if any(word in ('sh', '-c', DEV_UP_WRAPPER_SCRIPT) for word in words):
        raise Refused('a client command has no shell wrapper: the secret is read from the root env file by compose')
    prefix = tuple(testclient_prefix(stack_dir, repo_host_path))
    compose = words
    if compose[: len(prefix)] != prefix:
        raise Refused(
            'a client command names exactly the trusted client compose files, the snapshot and the root env file'
        )
    tail = compose[len(prefix) :]
    forbidden = _forbidden_lifecycle_words(tail)
    if forbidden:
        raise Refused(
            f'a client command names no compose file, project or env file and removes nothing: {", ".join(forbidden)}'
        )
    if tail in (SYSTEM_CLIENT_BUILD_TAIL, SEED_PRODUCER_RUN):
        return
    if tail[: len(SYSTEM_TESTS_RUN_HEAD)] == SYSTEM_TESTS_RUN_HEAD:
        paths = tail[len(SYSTEM_TESTS_RUN_HEAD) :]
        if 0 < len(paths) <= MAX_TEST_PATHS:
            for path in paths:
                _check_suite_path(path)
            return
    elif tail[: len(SEED_PRODUCER_RUN)] == SEED_PRODUCER_RUN and len(tail) == len(SEED_PRODUCER_RUN) + 2:
        flag, date = tail[len(SEED_PRODUCER_RUN) :]
        if flag == '--date' and check_seed_date(date) == date:
            return
    raise Refused('a client command is one of its fixed commands, with its fixed arguments, and no other')


def _testclient_steps(stack_dir: Path, repo_host_path: Path, *tails: Sequence[str]) -> list[Step]:
    """One Step per tail, each checked as built and run from the stack directory under the client's step env."""
    steps = []
    for tail in tails:
        argv = (*testclient_prefix(stack_dir, repo_host_path), *tail)
        check_testclient_argv(argv, stack_dir, repo_host_path)
        steps.append(Step(argv, stack_dir, builds(tail), env=testclient_step_env()))
    return steps


def system_tests_steps(stack_dir: Path, repo_host_path: Path, paths: Sequence[str]) -> list[Step]:
    """The test client, rebuilt from the snapshot (PATHS already resolved under tests/system), run against test mode."""
    return _testclient_steps(stack_dir, repo_host_path, [*SYSTEM_TESTS_RUN_HEAD, *paths])


# THE SEED PRODUCER'S ONE INVOCATION (ADR tj-4rr0la addendum 10 (1)), spelled exactly as make
# seed-dump spells it: test_client's entrypoint overridden to the image's own interpreter, the
# producer module, and --date only when one was given.
SEED_PRODUCER_RUN = (
    'run',
    '--rm',
    '-T',
    '--entrypoint',
    '/code/.venv/bin/python',
    'test_client',
    '-m',
    'data.store.seeds',
)


def seed_dump_steps(stack_dir: Path, repo_host_path: Path, date: str | None) -> list[Step]:
    """Build test_client from the snapshot, then run the producer in it; its stdout is the bundle.

    The build is a step of its own so the run is the one invocation make seed-dump also uses; both
    steps can build, so the runner makes BASE_IMAGES present before the first (addenda 14-15).
    """
    date_args = ['--date', date] if date is not None else []
    build, run = _testclient_steps(
        stack_dir, repo_host_path, SYSTEM_CLIENT_BUILD_TAIL, [*SEED_PRODUCER_RUN, *date_args]
    )
    return [build, replace(run, stdout_cap=MAX_BUNDLE_BYTES)]


# ---------------------------------------------------------------------------------------------
# THE DEV PROJECT'S TWO READ-ONLY COMMANDS (see DEV_PROJECT above). A separate prefix, a separate
# step builder and a separate argv check, sharing nothing with the file-based builders': a change to one
# cannot retarget the other, which is the failure this whole section is shaped to make impossible.
def dev_compose_prefix() -> list[str]:
    """`docker compose -p DEV_PROJECT`, and nothing else: no -f, no --project-directory, no --env-file.

    Four words. Everything the file-based prefixes add (-f, --project-directory) is content or
    configuration, and none of it may reach the label-only verbs.
    """
    return [DOCKER, 'compose', '-p', DEV_PROJECT]


def dev_file_prefix(repo_host_path: Path) -> list[str]:
    """The FILE-BASED dev prefix: `docker compose -p DEV_PROJECT --project-directory <host path> -f <trusted copy> ...`.

    For the verbs that build or create containers, which need the project loaded. The files are the
    TRUSTED copies baked into the image (DEV_COMPOSE_FILES in TRUSTED_COMPOSE_DIR), never the
    checkout's. --project-directory is the repository's HOST path (settings.repo_host_path), mounted
    read-only at the same path in this container: the CLI reads the build context and the project's env
    files there, and the daemon resolves every relative bind source there. There is no --env-file, so
    compose reads the root env file in that directory itself and the MCP's Python never opens it
    (addendum 23 (3)); the step builders pin the redirecting variables in the step environment.

    Label-only verbs use dev_compose_prefix() instead.
    """
    words = [*dev_compose_prefix(), '--project-directory', str(repo_host_path)]
    for name in DEV_COMPOSE_FILES:
        words += ['-f', str(TRUSTED_COMPOSE_DIR / name)]
    return words


def dev_tools_prefix(repo_host_path: Path) -> list[str]:
    """dev_file_prefix with the TRUSTED docker-compose.tools.yaml (pgAdmin) added, for tools alone.

    The Makefile's TOOLS_COMPOSE is the dev four plus the tools file; the MCP-only overlay stays LAST so
    its build.dockerfile wins, as in dev_file_prefix. No other verb loads this file: it carries `:?`
    guards on PGADMIN_EMAIL and PGADMIN_PASS that every command loading it would then need.
    """
    words = [*dev_compose_prefix(), '--project-directory', str(repo_host_path)]
    for name in DEV_TOOLS_COMPOSE_FILES:
        words += ['-f', str(TRUSTED_COMPOSE_DIR / name)]
    return words


def _trusted_file_prefix(repo_host_path: Path, names: Sequence[str]) -> list[str]:
    """`docker compose -p DEV_PROJECT --project-directory <host path>` and one trusted -f per name."""
    words = [*dev_compose_prefix(), '--project-directory', str(repo_host_path)]
    for name in names:
        words += ['-f', str(TRUSTED_COMPOSE_DIR / name)]
    return words


def testmode_prefix(repo_host_path: Path) -> list[str]:
    """The FILE-BASED TEST prefix, for test_up: the trusted base file and docker-compose.fake.yaml, as make system-launch's up."""
    return _trusted_file_prefix(repo_host_path, TEST_COMPOSE_FILES)


def testmode_build_prefix(repo_host_path: Path) -> list[str]:
    """testmode_prefix plus the trusted MCP-only overlay, for test_build alone (build.dockerfile, nothing else)."""
    return _trusted_file_prefix(repo_host_path, TEST_BUILD_COMPOSE_FILES)


def check_dev_argv(argv: Sequence[str]) -> None:
    """Refuse any dev argv but `docker compose -p DEV_PROJECT <ps|logs> ...` with no steering option.

    Checked on the words that go to the daemon, not on the builders' intent, and raised as Refused so
    a would-be violation is audited and no subprocess runs. Any other project name is refused
    here: these verbs reach DEV_PROJECT and nothing else.

    All three spellings of a steering option are refused after the prefix: separated (`-f x`),
    attached long (`--env-file=x`) and attached short (`-fx`, `-px`).
    """
    words = list(argv)
    head = dev_compose_prefix()
    if words[: len(head)] != head:
        raise Refused(f'a dev verb runs only `docker compose -p {DEV_PROJECT}`')
    tail = words[len(head) :]
    if not tail or tail[0] not in DEV_READ_COMMANDS:
        allowed = ', '.join(sorted(DEV_READ_COMMANDS))
        raise Refused(
            f'a dev verb runs only the read-only {allowed}; nothing that creates, changes or removes anything'
        )
    steering = [
        word
        for word in tail
        if word.split('=', 1)[0] in DEV_FORBIDDEN_OPTIONS or word.startswith(DEV_FORBIDDEN_SHORT_PREFIXES)
    ]
    if steering:
        raise Refused(f'a dev verb names no compose file, project or env file: {", ".join(steering)}')


def _dev_steps(stack_dir: Path, *tails: Sequence[str]) -> list[Step]:
    """One Step per tail, each checked by check_dev_argv and none of them able to build.

    cwd is the stack directory: outside the repository, holding no compose file and no `.env`, so
    even a compose that tried to discover one would find nothing. The snapshot is not used -- these
    verbs read no worktree at all.
    """
    prefix = dev_compose_prefix()
    steps = []
    for tail in tails:
        argv = tuple(prefix + list(tail))
        check_dev_argv(argv)
        steps.append(Step(argv, stack_dir))
    return steps


def dev_ps_steps(stack_dir: Path) -> list[Step]:
    """The dev project's containers, stopped ones included, with their health."""
    return _dev_steps(stack_dir, ['ps', '--all'])


def dev_logs_steps(stack_dir: Path, service: str, tail: int) -> list[Step]:
    """The last `tail` lines of one dev service's log. Never --follow: a verb returns."""
    return _dev_steps(stack_dir, ['logs', '--no-color', '--tail', str(tail), service])


# ---------------------------------------------------------------------------------------------
# THE DEV LIFECYCLE'S CHECKS (tj-iee6sn.6; ADR tj-4rr0la addenda 22-24; owner rulings D5-D7 on
# tj-iee6sn.1). Checks and helpers only: the verbs that use them (down, restart, build, up, migrate)
# come with their own beads. Each check is deliberately small and refuses only what a normal dev
# checkout never produces.
#
# THE ARGV TABLE. Every lifecycle command is one row, whole: a word the table does not hold is
# refused, so nothing is allowed by omission. The forbidden-word scan runs first only so the refusal
# can name what was wrong.
_DEV_LABEL_ONLY = 'label-only'
_DEV_FILE_BASED = 'file-based'
_DEV_TOOLS_FILE_BASED = 'file-based+tools'
_TEST_FILE_BASED = 'file-based+test'
_TEST_BUILD_FILE_BASED = 'file-based+test-build'
DEV_RESTART_SERVICES = ('data_store', 'data_ingest', 'web')
DEV_BUILD_TAIL = ('build', 'data_store', 'data_ingest', 'web')
DEV_UP_TAIL = ('up', '-d', '--wait', '--wait-timeout', '300', 'postgres', 'data_store', 'data_ingest', 'web')
# `make dev-tools` is `up -d pgadmin`; --wait is the MCP's addition (the verb returns when pgAdmin runs).
# postgres is not named: compose starts it through pgadmin's depends_on, as make does.
DEV_TOOLS_TAIL = ('up', '-d', '--wait', 'pgadmin')
# TEST MODE. `make test-build` is `build data_store data_ingest` over the test set; `make system-launch`'s
# up is `up -d --wait --wait-timeout 300` over the same set WITHOUT service names, so it starts what the
# files define (postgres, data_store, data_ingest) -- the words are the Makefile's, nothing added.
TEST_BUILD_TAIL = ('build', 'data_store', 'data_ingest')
TEST_UP_TAIL = ('up', '-d', '--wait', '--wait-timeout', '300')
DEV_ALEMBIC_PROGRAM = '/code/.venv/bin/alembic'
DEV_MIGRATE_TAIL = ('run', '--rm', '--no-deps', 'data_store', DEV_ALEMBIC_PROGRAM, 'upgrade', 'head')
DEV_LIFECYCLE_COMMANDS: dict[tuple[str, ...], str] = {
    ('down',): _DEV_LABEL_ONLY,
    **{('restart', service): _DEV_LABEL_ONLY for service in DEV_RESTART_SERVICES},
    DEV_BUILD_TAIL: _DEV_FILE_BASED,
    DEV_UP_TAIL: _DEV_FILE_BASED,
    DEV_TOOLS_TAIL: _DEV_TOOLS_FILE_BASED,
    TEST_BUILD_TAIL: _TEST_BUILD_FILE_BASED,
    TEST_UP_TAIL: _TEST_FILE_BASED,
    DEV_MIGRATE_TAIL: _DEV_FILE_BASED,
}
# The one-off migrate over the TEST set is the SAME words as DEV_MIGRATE_TAIL under a different prefix, so it
# cannot be a second key of the table above (one kind per tail): it is its own row, accepted only under
# testmode_prefix (the base file and docker-compose.fake.yaml, as run_migrations.sh's TEST definition).
TEST_ONEOFF_COMMANDS: dict[tuple[str, ...], str] = {DEV_MIGRATE_TAIL: _TEST_FILE_BASED}
# Data-losing words, refused wherever they appear after the prefix, on top of DEV_FORBIDDEN_OPTIONS
# (file, project, directory, env file, profile). -V is `up`'s --renew-anon-volumes.
DEV_LIFECYCLE_FORBIDDEN = DEV_FORBIDDEN_OPTIONS | frozenset(
    {'-v', '-V', '--volumes', '--renew-anon-volumes', '--rmi', '--remove-orphans', 'prune'}
)


def _forbidden_lifecycle_words(tail: Sequence[str]) -> list[str]:
    """Every word of TAIL that spells a forbidden option, in any of its spellings.

    Separated (`-v`), attached long (`--volumes=true`), attached short (`-fx`) and a short cluster
    (`-dv`: a single-dash word of several letters holding v or V).
    """
    found = []
    for word in tail:
        if (
            word.split('=', 1)[0] in DEV_LIFECYCLE_FORBIDDEN
            or word.startswith(DEV_FORBIDDEN_SHORT_PREFIXES)
            or (word.startswith('-') and not word.startswith('--') and ('v' in word or 'V' in word))
        ):
            found.append(word)
    return found


def check_dev_lifecycle_argv(argv: Sequence[str], repo_host_path: Path | None = None) -> None:
    """Refuse any dev lifecycle argv but one row of DEV_LIFECYCLE_COMMANDS under its own prefix, whole.

    A label-only row runs under dev_compose_prefix() alone; a file-based row under
    dev_file_prefix(repo_host_path), so REPO_HOST_PATH is required to accept one; the tools row
    only under dev_tools_prefix (the tools file added), the test_build row only under testmode_build_prefix
    (the fake and test-overlay files, the only set that can build :test), the test_up row only under
    testmode_prefix, and no other row under any of them. Checked on the
    words that go to the daemon, not on the builders' intent. check_dev_argv stays as it is, for the
    read-only ps and logs.
    """
    words = list(argv)
    label_prefix = dev_compose_prefix()
    if words[: len(label_prefix)] != label_prefix:
        raise Refused(f'a dev lifecycle verb runs only `docker compose -p {DEV_PROJECT}`')
    file_based = words[len(label_prefix) : len(label_prefix) + 1] == ['--project-directory']
    kind_wanted = _DEV_LABEL_ONLY
    if file_based:
        if repo_host_path is None:
            raise Refused('a file-based dev lifecycle command needs the repository host path to be checked')
        # Longest first: testmode_prefix is a word-prefix of testmode_build_prefix, so the build set must be
        # tried before the up set (the up set would otherwise read the overlay's -f as a forbidden tail word).
        candidates = (
            (dev_tools_prefix(repo_host_path), _DEV_TOOLS_FILE_BASED),
            (testmode_build_prefix(repo_host_path), _TEST_BUILD_FILE_BASED),
            (testmode_prefix(repo_host_path), _TEST_FILE_BASED),
            (dev_file_prefix(repo_host_path), _DEV_FILE_BASED),
        )
        for candidate, candidate_kind in candidates:
            if words[: len(candidate)] == candidate:
                prefix, kind_wanted = candidate, candidate_kind
                break
        else:
            raise Refused('a file-based dev lifecycle command names exactly the trusted dev compose files')
    else:
        prefix = label_prefix
    tail = words[len(prefix) :]
    forbidden = _forbidden_lifecycle_words(tail)
    if forbidden:
        raise Refused(
            'a dev lifecycle command removes no volume, image or orphan and names no compose file, project '
            f'or env file: {", ".join(forbidden)}'
        )
    kind = DEV_LIFECYCLE_COMMANDS.get(tuple(tail))
    if kind != kind_wanted and TEST_ONEOFF_COMMANDS.get(tuple(tail)) != kind_wanted:
        raise Refused('a dev lifecycle command is one of its fixed commands, with its fixed arguments, and no other')


# THE ENVIRONMENT. compose reads its own environment AHEAD of the project's env files, so a verb that
# lets compose read them (D5: the MCP never opens them) pins the variables that would redirect it.
# The in-repo defaults are the compose files' own (`${ROOT_ENV_FILE:-.env}` and its two siblings),
# resolved against --project-directory.
DEV_ENV_FILES = ('.env', 'server/data/store/.env', 'server/data/ingest/.env')
_DEV_ENV_FILE_VARIABLES = (
    ('ROOT_ENV_FILE', '.env'),
    ('STORE_ENV_FILE', './server/data/store/.env'),
    ('INGEST_ENV_FILE', './server/data/ingest/.env'),
)
_DEV_STEERING_ENV = (
    ('COMPOSE_PROJECT_NAME', DEV_PROJECT),
    ('COMPOSE_FILE', ''),
    ('COMPOSE_PATH_SEPARATOR', ':'),
    ('COMPOSE_ENV_FILES', ''),
    ('COMPOSE_PROFILES', ''),
    ('COMPOSE_REMOVE_ORPHANS', 'false'),
    ('COMPOSE_IGNORE_ORPHANS', 'false'),
    # The build engine: a COMPOSE* variable a project .env could otherwise set and compose would
    # promote when unset. false is the classic build path, the one make dev-build takes.
    ('COMPOSE_BAKE', 'false'),
)
# What a verb that creates nothing (a build) needs for compose to interpolate the dev files: the
# `:?` variables set to a throwaway word and every env-file variable to /dev/null, so no env file
# is read and no secret is needed.
_DEV_PLACEHOLDER_ENV = (
    ('POSTGRES_PASS', 'unused'),
    ('INSTANCE_WRITE_SECRET', 'unused'),
    # The container half of the override's publish entries ('127.0.0.1:${DATA_STORE_PORT}:${APP_INTERNAL_PORT}');
    # empty, compose refuses the entry. The .env.default value.
    ('APP_INTERNAL_PORT', '80'),
    # Interpolated in fields compose validates at config time: postgres container_name (empty is
    # refused) and the override's published host ports (an empty one makes the entry invalid). The
    # .env.default values; POSTGRES_USER and POSTGRES_DB_NAME only warn when unset, set to silence it.
    ('DATABASE_NAME', 'db'),
    ('DATABASE_PORT', '5432'),
    ('DATA_STORE_PORT', '8080'),
    ('DATA_INGEST_PORT', '8081'),
    ('POSTGRES_USER', 'postgres'),
    ('POSTGRES_DB_NAME', 'postgres'),
    *((name, '/dev/null') for name, _ in _DEV_ENV_FILE_VARIABLES),
)


def dev_pinned_step_env() -> tuple[tuple[str, str], ...]:
    """The step environment of a verb that lets compose read the project's env files: pinned, no value read."""
    return (*_DEV_ENV_FILE_VARIABLES, *_DEV_STEERING_ENV)


def dev_placeholder_step_env() -> tuple[tuple[str, str], ...]:
    """The step environment of a verb that creates nothing (build): placeholders, env files at /dev/null.

    COMPOSE_ENV_FILES is /dev/null here, not the shared steering value '': compose may read an empty
    value as unset and then load <project-directory>/.env for interpolation, and build reads no env
    file (D5). The key appears once, in the shared steering position.
    """
    steering = tuple((name, '/dev/null' if name == 'COMPOSE_ENV_FILES' else value) for name, value in _DEV_STEERING_ENV)
    return (*_DEV_PLACEHOLDER_ENV, *steering)


def testmode_pinned_step_env() -> tuple[tuple[str, str], ...]:
    """test_up's step environment: the pinned env (compose reads the env files itself) plus DATA_DIR on the test dir.

    DATA_DIR is a literal, never read from the owner's env file: process env outranks the project's
    .env, so postgres lands on ./volume_data/test and never on the owner's dev data.
    """
    return (*dev_pinned_step_env(), ('DATA_DIR', TEST_DATA_DIR))


def testmode_placeholder_step_env() -> tuple[tuple[str, str], ...]:
    """test_build's step environment: build's placeholders (no env file is read) plus DATA_DIR on the test dir."""
    return (*dev_placeholder_step_env(), ('DATA_DIR', TEST_DATA_DIR))


def _lstat_below(root: Path, relative: str) -> os.stat_result | None:
    """Lstat every component of RELATIVE under ROOT, root down; Refused on a symlink, None when it is missing.

    No component is opened or followed. A component that is not a directory (ENOTDIR) counts as
    missing, as the path then names nothing; any other error is Refused rather than guessed past.
    A refusal names the component that IS the link, relative to ROOT, and the path it was found on
    the way to; a link at the leaf is named once.
    """
    current = root
    found: os.stat_result | None = None
    parts = relative.split('/')
    for index, part in enumerate(parts):
        current = current / part
        try:
            found = os.lstat(current)
        except (FileNotFoundError, NotADirectoryError):
            return None
        except OSError as error:
            raise Refused(f'{relative} could not be checked: {error.strerror}') from error
        if stat.S_ISLNK(found.st_mode):
            link = '/'.join(parts[: index + 1])
            on_the_way = '' if link == relative else f' (on the way to {relative})'
            raise Refused(f'a symlink at {link}{on_the_way}: remove the link, or start the stack yourself')
    return found


def env_file_preflight(repo_root: Path) -> None:
    """D5: lstat the three in-repo env files WITHOUT opening them; refuse a symlink or a non-regular file.

    A missing optional file is allowed, as compose allows it. The files' contents are never read here:
    compose reads them itself.
    """
    for relative in DEV_ENV_FILES:
        found = _lstat_below(repo_root, relative)
        if found is not None and not stat.S_ISREG(found.st_mode):
            raise Refused(f'{relative} is not a regular file: remove it, or start the stack yourself')


# D6, MINIMAL. The in-repo bind sources of the trusted dev files; a validator test pins this list
# against the parsed files, so the runtime stays a plain list. The Docker daemon resolves a bind
# source on the host as root and follows symlinks, so a planted link at (or above) a source would
# mount whatever it points at. Residual, recorded: the milliseconds between this check and the mount,
# and DATA_DIR, which lies outside the repository.
DEV_BIND_SOURCES = (
    'server/data/store/app',
    'server/data/ingest/app',
    'server/common',
    'server/routers',
    'server/schemas',
    'gen/proto/python',
    'server/data/store/alembic.ini',
    'server/data/store/migrations',
    'web/src',
    'gen/proto',
)


def check_dev_bind_sources(repo_root: Path, sources: Sequence[str] = DEV_BIND_SOURCES) -> None:
    """Refuse when any component of any SOURCES path under REPO_ROOT is a symlink (default: DEV_BIND_SOURCES).

    Run right before a verb that creates or starts containers. A MISSING source is not refused:
    behaviour for a checkout without it is unchanged.
    """
    for relative in sources:
        _lstat_below(repo_root, relative)


# D6 for test mode: every in-repo bind source of the test set (docker-compose.yaml binds data_store's
# alembic.ini and migrations; docker-compose.fake.yaml mounts tests/fakes read-only into data_ingest;
# docker-compose.agent-test.yaml mounts nothing). The only other bind is postgres' data directory,
# under ./volume_data/test, the residual DEV_BIND_SOURCES' comment records.
TEST_BIND_SOURCES = ('server/data/store/alembic.ini', 'server/data/store/migrations', 'tests/fakes')


# D7 GENERALISED INTO THE MODE GUARD (ADR tj-4rr0la addenda 26-27, 29; tj-iee6sn.24, .31). Dev and test
# run in the explicit project trader_joe_dev and prod in the default one, so a prod stack is no longer
# this project; should a prod-labelled container ever be found here anyway, a state-changing verb would
# act on it, so it is refused. One `docker ps` over the config_files label, over the project's NON-one-off
# containers (alembic runs and test_client are `run` containers and are ignored), classified by the
# basename of each container's files exactly as run_migrations.sh's classify_config_files does:
#   DEV   the override present, the fake file absent        (make dev-up, MCP up)
#   TEST  the fake file present, the override absent        (make system-launch, MCP test_up)
#   PROD  anything else: neither file, both together (no target launches that pair), a malformed or
#         missing label
# Any PROD container, or two modes at once, refuses every state-changing verb; a mode a verb does not
# act on refuses it with that verb's own hint. tj-oxxl8a (an explicit prod project name) is the
# structural fix.
DEV_OVERRIDE_FILE = 'docker-compose.override.yaml'
TEST_FAKE_FILE = 'docker-compose.fake.yaml'
DEV_CONFIG_FILES_LABEL = 'com.docker.compose.project.config_files'
MODE_DEV = 'DEV'
MODE_TEST = 'TEST'
MODE_PROD = 'PROD'
# The modes each verb acts on; None is an empty project. up and test_up stop the project first, so
# they act on any non-PROD state. down and restart act on whatever launched mode is up.
# tools is dev-only; migrate and migrate_status act on DEV or TEST (an empty project is refused).
MODES_ANY = frozenset({MODE_DEV, MODE_TEST, None})
MODES_DEV_OR_EMPTY = frozenset({MODE_DEV, None})
MODES_DEV_OR_TEST = frozenset({MODE_DEV, MODE_TEST})
# run_system_tests and seed_dump act on TEST mode alone: they write the database, and only test mode's own
# launch (./volume_data/test, the fake broker) makes that database disposable.
MODES_TEST = frozenset({MODE_TEST})
SYSTEM_SUITE_MODE_HINT = 'the system suite runs only in test mode: call test_up first; your dev data is not touched'


def dev_prod_guard_argv() -> tuple[str, ...]:
    """`docker ps -a` over every NON-one-off container of DEV_PROJECT, printing the config_files label only."""
    return (
        DOCKER,
        'ps',
        '-a',
        '--filter',
        f'label={DEV_LABEL_PROJECT}={DEV_PROJECT}',
        '--filter',
        f'label={DEV_LABEL_ONEOFF}=False',
        '--format',
        f'{{{{.Label "{DEV_CONFIG_FILES_LABEL}"}}}}',
    )


def check_dev_prod_guard_argv(argv: Sequence[str]) -> None:
    """Refuse any argv but dev_prod_guard_argv(), word for word."""
    if tuple(argv) != dev_prod_guard_argv():
        raise Refused('the mode guard is `docker ps` over the project label, printing one label, and nothing else')


def dev_prod_guard_step(stack_dir: Path) -> Step:
    """The guard's one docker call, checked as built."""
    argv = dev_prod_guard_argv()
    check_dev_prod_guard_argv(argv)
    return Step(argv, stack_dir)


def classify_config_files(label: str) -> str:
    """A port of run_migrations.sh's classify_config_files: 'DEV', 'TEST', 'PROD', or 'UNKNOWN'.

    DEV: well formed, some entry's file name is exactly docker-compose.override.yaml and none is
    docker-compose.fake.yaml. TEST: the reverse. PROD: well formed, and neither is present. UNKNOWN:
    empty, or malformed (an empty or relative entry, or a newline), or BOTH files (no target launches
    that pair). A path holding a comma is malformed: fail closed. The script says UNKNOWN where
    classify_mode says PROD; a test pins that the two agree on every case.
    """
    if not label or '\n' in label:
        return 'UNKNOWN'
    entries = label.split(',')
    if any(not entry or not entry.startswith('/') for entry in entries):
        return 'UNKNOWN'
    names = {entry.rsplit('/', 1)[-1] for entry in entries}
    has_override, has_fake = DEV_OVERRIDE_FILE in names, TEST_FAKE_FILE in names
    if has_override and has_fake:
        return 'UNKNOWN'
    if has_override:
        return MODE_DEV
    return MODE_TEST if has_fake else MODE_PROD


def classify_mode(label: str) -> str:
    """The mode of one container's config_files label: DEV, TEST or PROD (UNKNOWN, malformed and missing are PROD)."""
    verdict = classify_config_files(label)
    return verdict if verdict in (MODE_DEV, MODE_TEST) else MODE_PROD


def check_mode_guard(
    exit_status: int | None, stdout: bytes, accepted: frozenset[str | None] = MODES_ANY, unaccepted: str = ''
) -> str | None:
    """The mode the project is in (DEV, TEST, or None for an empty project); Refused unless ACCEPTED holds it.

    Refused on any PROD container, on DEV and TEST containers together, and on a mode outside
    ACCEPTED (UNACCEPTED is the verb's own hint). Fail closed on anything else unexpected: a failed
    command, bytes that are not UTF-8, a missing final newline, a blank line (a container with no label).
    """
    prod = (
        f'project {DEV_PROJECT} holds a container not started from the dev or test files (a prod launch?): '
        'stop it with make dev-down; tj-oxxl8a'
    )
    if exit_status != 0:
        raise Refused(f'could not list the containers of project {DEV_PROJECT}, so nothing was changed')
    mode: str | None = None
    if stdout:
        try:
            text = stdout.decode('utf-8')
        except UnicodeDecodeError:
            raise Refused(prod) from None
        if not text.endswith('\n'):
            raise Refused(prod)
        modes = {classify_mode(line) for line in text[:-1].split('\n')}
        if MODE_PROD in modes:
            raise Refused(prod)
        if len(modes) > 1:
            raise Refused(
                f'project {DEV_PROJECT} holds dev and test containers together, so which mode is up cannot be '
                'told: stop them with make dev-down'
            )
        (mode,) = modes
    if mode not in accepted:
        raise Refused(unaccepted or f'{mode} mode is up, which this verb does not act on')
    return mode


# S6. The compose files, the Dockerfile and the two scripts the file-based dev verbs use are run from
# copies baked into the MCP image; when the checkout's differ, the verb would create containers from
# yesterday's files. Fail-safe: a difference can only refuse. docker-compose.tools.yaml is baked too
# (tools and the down fallback load it), so it is compared with the rest, and so are the test
# mode's two (docker-compose.fake.yaml, docker-compose.agent-test.yaml).
DEV_TRUSTED_COPIES = (
    *((name, name) for name in DEV_COMPOSE_FILES),
    ('docker-compose.tools.yaml', 'docker-compose.tools.yaml'),
    # Test mode's set (test_up, test_build): the fake-mode overlay and the MCP-only build overlay.
    *((name, name) for name in (*TEST_COMPOSE_FILES[1:], TEST_OVERLAY_FILE)),
    # The system-test client's two (run_system_tests, seed_dump): the client file and the MCP-only overlay.
    *((name, name) for name in SYSTEM_CLIENT_COMPOSE_FILES[1:]),
    ('Dockerfile', 'Dockerfile'),
    ('tools/dev_instance_secret.sh', 'tools/dev_instance_secret.sh'),
    ('tools/source_digest.sh', 'tools/source_digest.sh'),
)
_TRUSTED_COPY_MAX_BYTES = 1024 * 1024


def _read_regular(path: Path) -> bytes | None:
    """The bytes of a regular file opened without following a link; None when absent, special or too big."""
    try:
        descriptor = os.open(path, _O_NOFOLLOW_FILE)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        data = os.read(descriptor, _TRUSTED_COPY_MAX_BYTES + 1)
    finally:
        os.close(descriptor)
    return None if len(data) > _TRUSTED_COPY_MAX_BYTES else data


def check_trusted_copies_current(repo_root: Path, trusted_dir: Path = TRUSTED_COMPOSE_DIR) -> None:
    """Refuse when any DEV_TRUSTED_COPIES file baked into TRUSTED_DIR differs byte for byte from REPO_ROOT's."""
    for checkout_name, baked_name in DEV_TRUSTED_COPIES:
        checkout = _read_regular(repo_root / checkout_name)
        if checkout is None or checkout != _read_regular(trusted_dir / baked_name):
            raise Refused(
                f'the MCP image is stale: {checkout_name} differs from the checkout (or is missing); '
                'run make agent-mcp-rebuild on the host, then reconnect the MCP'
            )


# S10. A looping agent must be stopped, not served: starting data_ingest can trigger the daily refresh
# against the owner's paper quota. In memory, so an MCP restart resets it.
DEV_BUDGET_LIMIT = 12
DEV_BUDGET_WINDOW_SECONDS = 3600.0


class DevBudget:
    """At most DEV_BUDGET_LIMIT state-changing dev verbs per rolling DEV_BUDGET_WINDOW_SECONDS."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._calls: list[float] = []

    def charge(self) -> None:
        """Count one verb, or Refused when the window already holds the limit; a refused call is not counted."""
        now = self._clock()
        with self._lock:
            self._calls = [at for at in self._calls if now - at < DEV_BUDGET_WINDOW_SECONDS]
            if len(self._calls) >= DEV_BUDGET_LIMIT:
                raise Refused(
                    f'{DEV_BUDGET_LIMIT} state-changing dev verbs in the last hour is the budget; wait, '
                    'or run the make target yourself'
                )
            self._calls.append(now)


# ---------------------------------------------------------------------------------------------
# THE LABEL-ONLY MUTATING VERBS, down AND restart (tj-iee6sn.7; owner rulings D1, D2, D6, D7).
# `docker compose -p trader_joe_dev down` and `... restart <service>` pass no -f, so compose may search the
# step's working directory (the stack directory) and every parent for a compose file, and reads a .env
# beside one. The step's environment is pinned (dev_label_only_step_env) and this refuses, before the
# step, if anything of that kind is there. lstat only, nothing is opened.
_STEERING_NAMES = (
    'compose.yaml',
    'compose.yml',
    'compose.override.yaml',
    'compose.override.yml',
    'docker-compose.yaml',
    'docker-compose.yml',
    'docker-compose.override.yaml',
    'docker-compose.override.yml',
    '.env',
)


def check_dev_steering_files(stack_dir: Path) -> None:
    """Refuse when STACK_DIR or any parent up to / holds a compose file (or an override of one) or a .env.

    A name that exists in any form refuses (a file, a directory, a link, a dangling link), and so does
    an lstat error other than 'not there': fail closed. The refusal names the path.
    """
    for directory in (stack_dir, *stack_dir.parents):
        for name in _STEERING_NAMES:
            candidate = directory / name
            try:
                os.lstat(candidate)
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError as error:
                raise Refused(f'{candidate} could not be checked: {error.strerror}') from error
            raise Refused(f'{candidate} would steer compose: remove it, or run the make target yourself')


def dev_label_only_step_env() -> tuple[tuple[str, str], ...]:
    """The step environment of a label-only mutating verb: the steering variables pinned, no env file named."""
    return _DEV_STEERING_ENV


def validate_dev_restart_service(service: object) -> str:
    """The restart verb's service must be one of the closed DEV_RESTART_SERVICES (postgres is not one)."""
    if not isinstance(service, str) or service not in DEV_RESTART_SERVICES:
        raise Refused(f'service must be one of: {", ".join(DEV_RESTART_SERVICES)}')
    return service


def _dev_lifecycle_label_steps(stack_dir: Path, tail: Sequence[str]) -> list[Step]:
    """One label-only lifecycle Step, checked as built by check_dev_lifecycle_argv, with the pinned env."""
    argv = tuple(dev_compose_prefix() + list(tail))
    check_dev_lifecycle_argv(argv)
    return [Step(argv, stack_dir, env=dev_label_only_step_env())]


def dev_down_steps(stack_dir: Path) -> list[Step]:
    """`docker compose -p trader_joe_dev down`: containers and project networks removed; no -v, no --rmi, no --remove-orphans."""
    return _dev_lifecycle_label_steps(stack_dir, ['down'])


def dev_restart_steps(stack_dir: Path, service: str) -> list[Step]:
    """`docker compose -p trader_joe_dev restart <service>` for one member of DEV_RESTART_SERVICES."""
    return _dev_lifecycle_label_steps(stack_dir, ['restart', validate_dev_restart_service(service)])


# ---------------------------------------------------------------------------------------------
# build (tj-iee6sn.8; owner rulings D5, D8). `make dev-build` minus its host prerequisites, as a
# PLAIN call: file-based, so the project is loaded from the TRUSTED baked compose files with
# --project-directory the repository's host path. It creates and starts nothing and reads no env file
# (the step env is dev_placeholder_step_env), so it needs no prod guard, no symlink check and no
# steering walk: every -f is given, and a `:dev` image is not a container. What it does run, in
# order: the stale-copy check (S6), the generated-code freshness precondition, the budget, the two
# source digests from the TRUSTED source_digest.sh, then the build (the runner has the base images
# present first, as for every building step).
DEV_GENERATED_DIRS = ('gen/proto/python', 'gen/proto/ts')
# make test-build runs `proto` and no gen-proto-ts: test mode has no web service, so only the Python half.
TEST_GENERATED_DIRS = ('gen/proto/python',)
DEV_PROTO_SOURCE_DIR = 'proto'
DEV_DIGEST_SERVICES = (('data', 'store'), ('data', 'ingest'))
DEV_DIGEST_VARIABLES = ('SOURCE_DIGEST_DATA_STORE', 'SOURCE_DIGEST_DATA_INGEST')
# bash by absolute path: the step env's PATH is the runner's, and the script is `#! /bin/bash` anyway.
DEV_DIGEST_BASH = '/usr/bin/bash'
_DIGEST_PATTERN = re.compile(rb'[0-9a-f]{64}\n')
_GENERATE_HINT = 'run make proto gen-proto-ts in the root checkout'
TEST_GENERATE_HINT = 'run make proto in the root checkout'


def _newest_mtime(root: Path, suffix: str | None = None) -> float | None:
    """The newest mtime of a regular file under ROOT (SUFFIX filters by name); None when there is none.

    Walks without following a link; a symlinked file or directory is not counted, so a planted link
    cannot make a stale tree look fresh by pointing at a new file.
    """
    newest: float | None = None
    for directory, _, names in os.walk(root, followlinks=False):
        for name in names:
            if suffix is not None and not name.endswith(suffix):
                continue
            try:
                found = os.lstat(Path(directory) / name)
            except OSError:
                continue
            if stat.S_ISREG(found.st_mode) and (newest is None or found.st_mtime > newest):
                newest = found.st_mtime
    return newest


def check_generated_code_fresh(
    repo_root: Path, generated_dirs: Sequence[str] = DEV_GENERATED_DIRS, hint: str = _GENERATE_HINT
) -> None:
    """Refuse unless GENERATED_DIRS (default gen/proto/python and gen/proto/ts) hold files no older than the newest proto/**/*.proto.

    Read through the repository mount; nothing is generated here. make dev-build regenerates both
    first, and the MCP cannot run the generators, so a missing or older tree is refused naming the
    fix. A checkout with no .proto file at all has nothing to be older than.
    """
    newest_proto = _newest_mtime(repo_root / DEV_PROTO_SOURCE_DIR, '.proto')
    for relative in generated_dirs:
        generated = _newest_mtime(repo_root / relative)
        if generated is None:
            raise Refused(f'{relative} is missing or empty: {hint}')
        if newest_proto is not None and generated < newest_proto:
            raise Refused(f'{relative} is older than the newest .proto file: {hint}')


def dev_digest_argv(repo_host_path: Path, service_path: str, service_name: str) -> tuple[str, ...]:
    """The TRUSTED source_digest.sh, run over the repository's host path: bash <script> --context <path> <path> <name>."""
    return (DEV_DIGEST_BASH, str(TRUSTED_SOURCE_DIGEST), '--context', str(repo_host_path), service_path, service_name)


def check_dev_digest_argv(argv: Sequence[str], repo_host_path: Path) -> None:
    """Refuse any argv but one of the fixed digest commands, word for word."""
    allowed = {dev_digest_argv(repo_host_path, path, name) for path, name in DEV_DIGEST_SERVICES}
    if tuple(argv) not in allowed:
        raise Refused('the dev source digest is the trusted source_digest.sh over the repository, and nothing else')


def dev_digest_steps(stack_dir: Path, repo_host_path: Path) -> list[Step]:
    """One digest Step per DEV_DIGEST_SERVICES entry, each checked as built. cwd is the stack directory (the script moves itself)."""
    steps = []
    for path, name in DEV_DIGEST_SERVICES:
        argv = dev_digest_argv(repo_host_path, path, name)
        check_dev_digest_argv(argv, repo_host_path)
        steps.append(Step(argv, stack_dir))
    return steps


def parse_source_digest(exit_status: int | None, stdout: bytes) -> str | None:
    """The digest a digest step printed, or None unless it exited 0 and printed exactly 64 lowercase hex and a newline."""
    if exit_status != 0 or not _DIGEST_PATTERN.fullmatch(stdout):
        return None
    return stdout[:-1].decode('ascii')


def dev_build_step(stack_dir: Path, repo_host_path: Path, digests: Sequence[str]) -> Step:
    """`docker compose -p trader_joe_dev ... build data_store data_ingest web`, checked as built.

    The step env is the placeholder env plus the two digests, which are validated 64-hex values
    (parse_source_digest) and appear in no argv. Building, so the runner makes the bases present first.
    """
    if len(digests) != len(DEV_DIGEST_VARIABLES) or not all(
        _DIGEST_PATTERN.fullmatch(f'{digest}\n'.encode()) for digest in digests
    ):
        raise Refused('build needs exactly one valid source digest per image')
    argv = tuple(dev_file_prefix(repo_host_path) + list(DEV_BUILD_TAIL))
    check_dev_lifecycle_argv(argv, repo_host_path)
    env = (*dev_placeholder_step_env(), *zip(DEV_DIGEST_VARIABLES, digests, strict=True))
    return Step(argv, stack_dir, builds(DEV_BUILD_TAIL), env=env)


# ---------------------------------------------------------------------------------------------
# up (tj-iee6sn.9; owner rulings D1, D3, D5, D8; ADR tj-4rr0la addenda 22-24). `make dev-up` as a
# PLAIN call: the label-only down (single instance, D1), the devnet network, then the file-based up of
# postgres and the three app services. It never builds on purpose (a build cannot fit its 600 s), so the
# three :dev images must already exist.
DEV_IMAGES = ('trader_joe_data_store:dev', 'trader_joe_data_ingest:dev', 'trader_joe_web:dev')
DEV_NETWORK = 'trader_joe_devnet'
# The two images test mode builds and runs, the refs docker-compose.fake.yaml names (service order:
# data_store, data_ingest, so TEST_IMAGES pairs with DEV_DIGEST_SERVICES). test_build writes only these,
# test_up requires them, and nothing in the MCP names an image tagged :latest.
TEST_IMAGES = ('trader_joe_data_store:test', 'trader_joe_data_ingest:test')

# THE SECRET WRAPPER (F9). The one shell in the server. The up needs INSTANCE_WRITE_SECRET, which make
# derives with tools/dev_instance_secret.sh from the root env file; the MCP never reads that file, so a
# fixed `sh -c` does it inside the child and execs compose with the value in ITS environment only. The
# script is this constant, never formatted or interpolated: the helper and the env file arrive as $1 and
# $2, and the compose argv as the positional arguments after them, so no word of it is ever script text.
# An empty value in the env file makes the helper print "unused", as for make: reads work, writes are
# refused.
DEV_UP_WRAPPER_SCRIPT = (
    'INSTANCE_WRITE_SECRET="$(sh "$1" "$2")" || exit 1\nexport INSTANCE_WRITE_SECRET\nshift 2\nexec "$@"'
)
_DEV_UP_WRAPPER_WORDS = ('sh', '-c', DEV_UP_WRAPPER_SCRIPT, 'sh')


def dev_up_wrapper_head(repo_host_path: Path) -> tuple[str, ...]:
    """The six fixed leading words: sh -c SCRIPT sh <trusted helper> <repo host path>/.env."""
    return (*_DEV_UP_WRAPPER_WORDS, str(TRUSTED_DEV_INSTANCE_SECRET), str(repo_host_path / DEV_ENV_FILES[0]))


def _check_dev_wrapped_argv(
    argv: Sequence[str],
    repo_host_path: Path,
    tail: Sequence[str],
    name: str,
    prefix_for: Callable[[Path], list[str]] = dev_file_prefix,
) -> None:
    """Refuse any argv but the fixed wrapper head and then exactly the file-based compose command of TAIL.

    The remainder is checked by check_dev_lifecycle_argv, and must then be TAIL itself (the table also
    holds the other rows, which this wrapper never carries). NAME only words the refusals.
    """
    words = tuple(argv)
    head = dev_up_wrapper_head(repo_host_path)
    if words[: len(head)] != head:
        raise Refused(f'the dev {name} wrapper is its six fixed words and nothing else')
    compose = words[len(head) :]
    check_dev_lifecycle_argv(compose, repo_host_path)
    if compose != (*prefix_for(repo_host_path), *tail):
        raise Refused(f'the dev {name} wrapper runs the fixed {name} command, and no other')


def check_dev_up_argv(argv: Sequence[str], repo_host_path: Path) -> None:
    """Refuse any up argv but the fixed wrapper head and then exactly the file-based `up` compose command."""
    _check_dev_wrapped_argv(argv, repo_host_path, DEV_UP_TAIL, 'up')


def dev_up_step(stack_dir: Path, repo_host_path: Path, digests: Sequence[str]) -> Step:
    """The wrapped `docker compose ... up -d --wait --wait-timeout 300 postgres data_store data_ingest web`, checked as built.

    The step env is the full pinned env (compose reads the project's env files itself) plus the two
    validated source digests, so an image compose builds anyway is stamped. The secret is in no env
    here: the wrapper's child derives it.
    """
    if len(digests) != len(DEV_DIGEST_VARIABLES) or not all(
        _DIGEST_PATTERN.fullmatch(f'{digest}\n'.encode()) for digest in digests
    ):
        raise Refused('up needs exactly one valid source digest per image')
    argv = (*dev_up_wrapper_head(repo_host_path), *dev_file_prefix(repo_host_path), *DEV_UP_TAIL)
    check_dev_up_argv(argv, repo_host_path)
    env = (*dev_pinned_step_env(), *zip(DEV_DIGEST_VARIABLES, digests, strict=True))
    return Step(argv, stack_dir, builds(DEV_UP_TAIL), env=env)


def check_dev_tools_argv(argv: Sequence[str], repo_host_path: Path) -> None:
    """Refuse any argv but the fixed wrapper head and then exactly the tools-file `up -d --wait pgadmin` command."""
    _check_dev_wrapped_argv(argv, repo_host_path, DEV_TOOLS_TAIL, 'tools', dev_tools_prefix)


def dev_tools_step(stack_dir: Path, repo_host_path: Path) -> Step:
    """The wrapped `docker compose ... -f docker-compose.tools.yaml up -d --wait pgadmin`, checked as built.

    The step env is the pinned env: compose reads the root env file itself, PGADMIN_EMAIL and
    PGADMIN_PASS with it, so no credential is in an argv, the environment this server builds or its
    output (it reads none of them). An unset one is compose's `:?` refusal, which names the variable
    and never a value. No digest and not building: pgadmin is an image-only service, as is postgres.
    """
    argv = (*dev_up_wrapper_head(repo_host_path), *dev_tools_prefix(repo_host_path), *DEV_TOOLS_TAIL)
    check_dev_tools_argv(argv, repo_host_path)
    return Step(argv, stack_dir, env=dev_pinned_step_env())


def dev_image_steps(stack_dir: Path) -> list[Step]:
    """One fixed `docker image inspect --format {{.Id}}` per DEV_IMAGES entry: exit 0 when the image exists."""
    return [base_inspect_step(ref, stack_dir) for ref in DEV_IMAGES]


def dev_network_argv(create: bool) -> tuple[str, ...]:
    """`docker network inspect DEV_NETWORK`, or `docker network create --driver bridge DEV_NETWORK` (make dev-network)."""
    if create:
        return (DOCKER, 'network', 'create', '--driver', 'bridge', DEV_NETWORK)
    return (DOCKER, 'network', 'inspect', '--format', '{{.Name}}', DEV_NETWORK)


def check_dev_network_argv(argv: Sequence[str]) -> None:
    """Refuse any argv but one of the two dev_network_argv() commands, word for word."""
    if tuple(argv) not in (dev_network_argv(False), dev_network_argv(True)):
        raise Refused('the dev network step is inspect or create of the fixed network, and nothing else')


def dev_network_steps(stack_dir: Path) -> tuple[Step, Step]:
    """The inspect step and the create step (run only when the inspect fails), each checked as built."""
    steps = []
    for create in (False, True):
        argv = dev_network_argv(create)
        check_dev_network_argv(argv)
        steps.append(Step(argv, stack_dir))
    return steps[0], steps[1]


# ---------------------------------------------------------------------------------------------
# TEST MODE: test_build and test_up (tj-iee6sn.24; ADR tj-4rr0la addenda 26-27). `make test-build` and
# `make system-launch` as PLAIN calls, on the :test images only. Neither command line can reach :latest:
# the only file sets they are checked against hold docker-compose.fake.yaml, which names the :test refs.
# test_up needs no secret wrapper and no network step: the base and fake files carry no
# INSTANCE_WRITE_SECRET guard (that is the web files') and name no devnet; the services read their own
# env_file through compose, as under make system-launch.
def testmode_build_step(stack_dir: Path, repo_host_path: Path, digests: Sequence[str]) -> Step:
    """`docker compose -p trader_joe_dev ... build data_store data_ingest` over the test build set, checked as built.

    The step env is the placeholder env (no env file is read), DATA_DIR on the test dir and the two
    validated source digests, which appear in no argv. Building, so the runner makes the bases present first.
    """
    if len(digests) != len(DEV_DIGEST_VARIABLES) or not all(
        _DIGEST_PATTERN.fullmatch(f'{digest}\n'.encode()) for digest in digests
    ):
        raise Refused('test_build needs exactly one valid source digest per image')
    argv = tuple(testmode_build_prefix(repo_host_path) + list(TEST_BUILD_TAIL))
    check_dev_lifecycle_argv(argv, repo_host_path)
    env = (*testmode_placeholder_step_env(), *zip(DEV_DIGEST_VARIABLES, digests, strict=True))
    return Step(argv, stack_dir, builds(TEST_BUILD_TAIL), env=env)


def testmode_up_step(stack_dir: Path, repo_host_path: Path) -> Step:
    """`docker compose -p trader_joe_dev ... up -d --wait --wait-timeout 300` over the test set, checked as built.

    The step env is the pinned env (compose reads the project's env files itself) with DATA_DIR on the
    test dir. No digest: test_up requires both images to exist and match the checkout, so nothing is built.
    """
    argv = tuple(testmode_prefix(repo_host_path) + list(TEST_UP_TAIL))
    check_dev_lifecycle_argv(argv, repo_host_path)
    return Step(argv, stack_dir, builds(TEST_UP_TAIL), env=testmode_pinned_step_env())


def testmode_image_steps(stack_dir: Path) -> list[Step]:
    """One fixed `docker image inspect --format {{.Id}}` per TEST_IMAGES entry: exit 0 when the image exists."""
    return [base_inspect_step(ref, stack_dir) for ref in TEST_IMAGES]


# THE TEST DATABASE RAIL (run_system_tests, seed_dump; tj-iee6sn.20). The suite and the producer WRITE the
# database of the running postgres, and the owner's attestation (SYSTEM_TEST_DISPOSABLE_DB=1) lives on the
# make targets only. What stands in its place is a read of the running postgres container's own data mount:
# one fixed `docker inspect`, and the verb is refused unless that mount's host source is exactly
# <repo host path>/volume_data/test/postgres, the directory test_up pins. A dev postgres, or any other
# mount, is refused whatever mode label its container carries.
POSTGRES_DATA_TARGET = '/var/lib/postgresql/data'
_POSTGRES_MOUNT_FORMAT = (
    f'{{{{range .Mounts}}}}{{{{if eq .Destination "{POSTGRES_DATA_TARGET}"}}}}{{{{.Source}}}}{{{{end}}}}{{{{end}}}}'
)


def expected_test_postgres_mount(repo_host_path: Path) -> str:
    """The only data mount source a test-mode postgres has: <repo host path>/volume_data/test/postgres."""
    return str(repo_host_path / TEST_DATA_DIR / 'postgres')


def postgres_mount_argv(container_id: str) -> tuple[str, ...]:
    """`docker inspect` printing only the host source of the container's postgres data mount, by its checked id."""
    return (
        DOCKER,
        'inspect',
        '--type',
        'container',
        '--format',
        _POSTGRES_MOUNT_FORMAT,
        check_container_id(container_id),
    )


def check_postgres_mount_argv(argv: Sequence[str]) -> None:
    """Refuse any argv but postgres_mount_argv() over a well-formed container id, word for word."""
    words = tuple(argv)
    if len(words) != 7 or words != postgres_mount_argv(words[-1]):
        raise Refused('the postgres mount read is `docker inspect` of one container id, printing one mount source')


def postgres_mount_step(stack_dir: Path, container_id: str) -> Step:
    """The mount read of the running postgres container, checked as built."""
    argv = postgres_mount_argv(container_id)
    check_postgres_mount_argv(argv)
    return Step(argv, stack_dir)


def check_test_postgres_mount(exit_status: int | None, stdout: bytes, repo_host_path: Path) -> None:
    """Refused unless the running postgres's data mount source is exactly <repo host path>/volume_data/test/postgres.

    A failed read, bytes that are not UTF-8, an empty answer (no such mount) and any other source all refuse,
    with the same fix; the refusal names the expected directory and, for a different source, that source.
    """
    expected = expected_test_postgres_mount(repo_host_path)
    fix = f'the system suite writes only the postgres on {expected}: call test_up'
    if exit_status != 0:
        raise Refused(f'the running postgres container could not be read, so its data mount is unknown: {fix}')
    try:
        text = stdout.decode('utf-8')
    except UnicodeDecodeError:
        raise Refused(f'the postgres data mount is unreadable: {fix}') from None
    source = text[:-1] if text.endswith('\n') else text
    if source != expected:
        shown = f'{source[:200]!r}' if source else 'none'
        raise Refused(f'the running postgres data mount is {shown}, not the test directory: {fix}')


# ---------------------------------------------------------------------------------------------
# migrate (tj-iee6sn.10; owner ruling: ONE migrate, `make migrate`'s semantics, on whichever mode is up).
# run_migrations.sh selects the definition from the running postgres; here the mode guard has already told
# DEV from TEST (any PROD container, or both modes, was refused), and the MCP adds the script's own
# one-postgres / at-most-one-data_store counts. DEV runs the file-based one-off `run --rm --no-deps data_store
# alembic upgrade head` over the dev files, wrapped (compose interpolates the web files' INSTANCE_WRITE_SECRET
# `:?` guard even for a one-off of data_store, so it goes through up's fixed secret wrapper, under the
# pinned environment; make migrate loads only the first two files and needs no secret). TEST runs the same
# words over the TEST set (base + docker-compose.fake.yaml: the :test image, no web file, no wrapper), with
# DATA_DIR pinned on the test dir as test_up's is.
#
# NO ESCAPE HATCH, NO DRY RUN, NO CHECK. alembic check is not offered (make migrate-check covers it).
def migrate_selection(mode: str) -> str:
    """The selection line a migrate/migrate_status result starts with: the definition and mode chosen, as make migrate prints."""
    if mode == MODE_TEST:
        return (
            f'using the TEST definition (docker-compose.yaml + docker-compose.fake.yaml) of project {DEV_PROJECT} '
            'in TEST mode (the :test image; alembic.ini and revisions from the root checkout)'
        )
    return (
        f'using the DEV definition (docker-compose.yaml + docker-compose.override.yaml) of project {DEV_PROJECT} '
        'in DEV mode (revisions from the root checkout)'
    )


DEV_SOURCE_STAMP_LABEL = 'trader_joe.source.digest'
_STAMP_PATTERN = re.compile(r'[0-9a-f]{64}')
# The images whose build stamps the label (web is not stamped), and the one inspect format that reads it,
# from an image or from a container started from one.
STAMPED_IMAGES = (DEV_IMAGES[0], DEV_IMAGES[1], *TEST_IMAGES)
_STAMP_FORMAT = f'{{{{ with .Config.Labels }}}}{{{{ index . "{DEV_SOURCE_STAMP_LABEL}" }}}}{{{{ end }}}}'


def check_dev_migrate_argv(argv: Sequence[str], repo_host_path: Path) -> None:
    """Refuse any argv but the fixed wrapper head and then exactly the `alembic upgrade head` compose command."""
    _check_dev_wrapped_argv(argv, repo_host_path, DEV_MIGRATE_TAIL, 'migrate')


def _dev_alembic_step(stack_dir: Path, repo_host_path: Path, tail: Sequence[str], check: Callable[..., None]) -> Step:
    """The wrapped file-based one-off for TAIL, checked as built, under the pinned environment.

    Nothing is added to the environment: no digest (the image exists, so compose builds nothing) and
    no secret (the wrapper's child derives it).
    """
    argv = (*dev_up_wrapper_head(repo_host_path), *dev_file_prefix(repo_host_path), *tail)
    check(argv, repo_host_path)
    return Step(argv, stack_dir, builds(tail), env=dev_pinned_step_env())


def dev_migrate_step(stack_dir: Path, repo_host_path: Path) -> Step:
    """`... run --rm --no-deps data_store /code/.venv/bin/alembic upgrade head`, wrapped, checked as built."""
    return _dev_alembic_step(stack_dir, repo_host_path, DEV_MIGRATE_TAIL, check_dev_migrate_argv)


def check_testmode_migrate_argv(argv: Sequence[str], repo_host_path: Path) -> None:
    """Refuse any argv but exactly the `alembic upgrade head` compose command over the TEST set, with no wrapper."""
    words = tuple(argv)
    check_dev_lifecycle_argv(words, repo_host_path)
    if words != (*testmode_prefix(repo_host_path), *DEV_MIGRATE_TAIL):
        raise Refused('the test migrate is the fixed `alembic upgrade head` command over the test set, and no other')


def testmode_migrate_step(stack_dir: Path, repo_host_path: Path) -> Step:
    """`... -f docker-compose.yaml -f docker-compose.fake.yaml run --rm --no-deps data_store /code/.venv/bin/alembic upgrade head`, checked as built.

    The step env is the pinned env (compose reads the project's env files itself) with DATA_DIR on the test
    dir. No wrapper (the base and fake files need no secret) and no digest: the :test image exists and is
    verified by the caller, so compose builds nothing.
    """
    argv = (*testmode_prefix(repo_host_path), *DEV_MIGRATE_TAIL)
    check_testmode_migrate_argv(argv, repo_host_path)
    return Step(argv, stack_dir, builds(DEV_MIGRATE_TAIL), env=testmode_pinned_step_env())


def testmode_data_store_image_step(stack_dir: Path) -> Step:
    """`docker image inspect` of the data_store :test image: exit 0 when it exists (else compose would build it unstamped)."""
    return base_inspect_step(TEST_IMAGES[0], stack_dir)


def dev_data_store_image_step(stack_dir: Path) -> Step:
    """`docker image inspect` of the data_store :dev image: exit 0 when it exists (else compose would build it unstamped)."""
    return base_inspect_step(DEV_IMAGES[0], stack_dir)


def refuse_several_dev_containers(service: str, lookup_stdout: bytes) -> None:
    """Refuse when the lookup found more than one running SERVICE container: run_migrations.sh cannot tell which stack."""
    count = len([line for line in lookup_stdout.decode('utf-8', errors='replace').splitlines() if line.strip()])
    if count > 1:
        raise Refused(f'{count} running {service} containers in the {DEV_PROJECT} project; cannot tell which stack')


def stamp_image_fix(image: str) -> str:
    """The verb that rebuilds IMAGE: test_build for a :test image, build for a :dev one."""
    return 'call test_build' if image in TEST_IMAGES else 'call build'


def dev_stamp_argv(image: str = DEV_IMAGES[0]) -> tuple[str, ...]:
    """`docker image inspect` printing only the source-stamp label of IMAGE (default the data_store :dev image).

    IMAGE is one of STAMPED_IMAGES, the four images whose build stamps the label; anything else is Refused.
    """
    if image not in STAMPED_IMAGES:
        raise Refused(f'the source stamp is read from one of {", ".join(STAMPED_IMAGES)}, and no other image')
    return (DOCKER, 'image', 'inspect', '--format', _STAMP_FORMAT, image)


def check_dev_stamp_argv(argv: Sequence[str], image: str = DEV_IMAGES[0]) -> None:
    """Refuse any argv but dev_stamp_argv(IMAGE), word for word."""
    if tuple(argv) != dev_stamp_argv(image):
        raise Refused(f'the stamp read is `docker image inspect` of {image}, printing one label')


def dev_stamp_step(stack_dir: Path, image: str = DEV_IMAGES[0]) -> Step:
    """The stamp read's one docker call for IMAGE, checked as built."""
    argv = dev_stamp_argv(image)
    check_dev_stamp_argv(argv, image)
    return Step(argv, stack_dir)


def container_stamp_argv(container_id: str) -> tuple[str, ...]:
    """`docker inspect` printing only the source-stamp label of ONE RUNNING container, by its checked id."""
    return (DOCKER, 'inspect', '--type', 'container', '--format', _STAMP_FORMAT, check_container_id(container_id))


def check_container_stamp_argv(argv: Sequence[str]) -> None:
    """Refuse any argv but container_stamp_argv() over a well-formed container id, word for word."""
    words = tuple(argv)
    if len(words) != 7 or words != container_stamp_argv(words[-1]):
        raise Refused('the container stamp read is `docker inspect` of one container id, printing one label')


def container_stamp_step(stack_dir: Path, container_id: str) -> Step:
    """The stamp read of a running container (what the image it was started from was stamped with), checked as built."""
    argv = container_stamp_argv(container_id)
    check_container_stamp_argv(argv)
    return Step(argv, stack_dir)


def check_dev_image_stamp(
    exit_status: int | None, stdout: bytes, checkout_digest: str, image: str = DEV_IMAGES[0]
) -> None:
    """run_migrations.sh's verify_comparison_image, minus its escape hatch: Refused unless the stamp equals the digest.

    Three refusals, all naming the fix (stamp_image_fix(IMAGE): call build, or call test_build for a
    :test image). A failed read means the image is not there; a stamp that is not 64 lowercase hex (an
    unstamped image: one CI pulled and retagged, or one a bare compose build or run made implicitly) cannot be verified; a valid stamp
    that differs is a stale image. A match returns.
    """
    fix = stamp_image_fix(image)
    if exit_status != 0:
        raise Refused(f'the image {image} could not be read, so there is nothing to compare against: {fix}')
    stamp = stdout.decode('utf-8', errors='replace').strip()
    if image in TEST_IMAGES:
        if not _STAMP_PATTERN.fullmatch(stamp):
            raise Refused(
                f'CANNOT VERIFY: the image {image} carries no usable source stamp (absent is not a match): {fix}'
            )
        if stamp != checkout_digest:
            raise Refused(
                f'STALE: {image} was built from different source than this checkout '
                f'(image stamp {stamp}, checkout digest {checkout_digest}): {fix}'
            )
        return
    if not _STAMP_PATTERN.fullmatch(stamp):
        raise Refused(
            f'CANNOT VERIFY: the image {image} carries no usable source stamp, so what alembic check would '
            f'compare cannot be established; absent is not a match: {fix}'
        )
    if stamp != checkout_digest:
        raise Refused(
            f'STALE IMAGE: {image} was built from different source than this checkout '
            f'(image stamp {stamp}, checkout digest {checkout_digest}); alembic check would report phantom '
            f'drift: {fix}'
        )


def check_container_stamp(exit_status: int | None, stdout: bytes, checkout_digest: str, service: str) -> None:
    """The TEST gate of migrate: Refused unless the RUNNING SERVICE container's source stamp equals the checkout digest.

    A running container keeps the image it was started from, and test_build can have retagged :test
    since, so the container's own stamp is what proves the stack is current (a prod image bakes the code
    in). A failed read, a stamp that is not 64 lowercase hex and a stamp that differs all refuse with the
    same fix, and the digests of a mismatch are named.
    """
    fix = 'stale test stack: call test_build, then test_up'
    if exit_status != 0:
        raise Refused(f'the running {service} container could not be read, so its source stamp is unknown: {fix}')
    stamp = stdout.decode('utf-8', errors='replace').strip()
    if not _STAMP_PATTERN.fullmatch(stamp):
        raise Refused(f'CANNOT VERIFY: the running {service} container carries no usable source stamp: {fix}')
    if stamp != checkout_digest:
        raise Refused(
            f'STALE: the running {service} container was built from different source than this checkout '
            f'(container stamp {stamp}, checkout digest {checkout_digest}): {fix}'
        )


# ---------------------------------------------------------------------------------------------
# THE DEV PROJECT'S EXEC FAMILY (ADR tj-4rr0la addenda 22-23; owner ruling D9 on tj-iee6sn.1). A
# SECOND argv family beside the compose one above, held by check_dev_exec_argv() instead of
# check_dev_argv(): plain `docker exec`, never compose, so it reads no compose file and no env file.
#
# WHAT HOLDS IT. The agent names nothing: no container, no command, no SQL, no URL, no header. Every
# exec argv is built from the module constants below and re-checked as built. The target container
# comes from a label lookup (dev_lookup_argv), which must find exactly ONE RUNNING container.
#
# The labels compose puts on every container it creates. oneoff=False keeps `run` containers out.
DEV_LABEL_PROJECT = 'com.docker.compose.project'
DEV_LABEL_SERVICE = 'com.docker.compose.service'
DEV_LABEL_ONEOFF = 'com.docker.compose.oneoff'
# `docker ps --format` prints the short container id, 12 lowercase hex digits; the id is checked
# before it is handed to exec, so nothing but an id can take its place in the argv.
_CONTAINER_ID = re.compile(r'^[0-9a-f]{12,64}$')

# Read-only for every statement of the session, whatever the SQL: psql reads PGOPTIONS (libpq).
DEV_EXEC_PGOPTIONS = 'PGOPTIONS=-c default_transaction_read_only=on'
# The option sets an exec may carry, each whole. Which one goes with which command is DEV_EXEC_COMMANDS
# below: the options are bound to the command, never chosen separately, so the database script cannot
# run as root or without the read-only option, nor the alembic wrapper as user postgres. -i, -t,
# --privileged, --env-file, --env, any other -e and the attached spellings (-ePATH=x, -uroot) have no
# set and so are refused.
_DEV_EXEC_OPTIONS_POSTGRES = ('-u', 'postgres', '-e', DEV_EXEC_PGOPTIONS)
_DEV_EXEC_OPTIONS_CODE = ('-w', '/code')

# db_tables: the tables whose presence it reports (to_regclass, so a missing one is false, not an
# error), in the order of the models under server/data/store/app/database/models. The schema is the
# default, public: no model names another. alembic_version is alembic's own bookkeeping table.
DEV_DB_TABLES = (
    'dataset_read',
    'dataset_read_daily',
    'dataset_override_log',
    'dataset_deletion',
    'ingest_run',
    'store_dataset_entry',
    'stock_market_activity',
    'alembic_version',
)
DEV_DB_VERSION_TABLE = 'alembic_version'
_DEV_DB_TABLES_QUERY = ' UNION ALL '.join(
    f"SELECT '{name}', to_regclass('public.{name}') IS NOT NULL" for name in DEV_DB_TABLES
)
# The second statement runs only when the first reported alembic_version present.
_DEV_DB_VERSION_QUERY = "SELECT 'version_num', version_num FROM public.alembic_version ORDER BY version_num"
# Run INSIDE the postgres container by `sh -c`, as user postgres, in a read-only session (PGOPTIONS).
# POSTGRES_USER and POSTGRES_DB expand in the container and are never printed. Every byte printed
# goes through redact: the values of the container's own secret variables, and of any variable
# holding a database URL, become <redacted> before they leave the container. The scrub contract
# (tj-4kyqk9), shared by every exec wrapper that pipes through redact: the secrets are the non-empty
# values, plus each non-empty line of a multi-line value as its own secret, de-duplicated and redacted
# over the whole output (awk reads it all, so a value spanning lines still matches); an empty value
# is never a secret. The SQL is a constant with no placeholder: nothing is interpolated.
_DEV_REDACT_NAMES = 'INSTANCE_WRITE_SECRET POSTGRES_PASS POSTGRES_PASSWORD ALPACA_API_KEY ALPACA_API_SECRET'
# The scrub is the UNION of every match span: every occurrence of every secret is found over the
# ORIGINAL text (stepping one character past each match start, so self-overlapping occurrences are
# all found), overlapping and abutting spans merge, and each merged span becomes ONE <redacted>.
# Order of secrets is irrelevant; over-redacting is acceptable, under-redacting is not. The input is
# held as lines joined pairwise (O(n log n), no per-line growth of one string) and scanned in
# windows so the cost stays near-linear; output is written piece by piece. When the caller sets
# REDACT_TRUNCATED=1 (a constant signal, never a value) the input was cut by a byte bound, so the
# longest trailing suffix that is a proper prefix of any secret is redacted too. Off by default.
_DEV_REDACT_FUNCTION = r"""redact() { awk 'function put(s) { if (s != "" && !(s in seen)) { seen[s] = 1; secrets[++m] = s } }
function add(v,  c, parts, q) { if (v == "") return; put(v); c = split(v, parts, "\n"); if (c > 1) for (q = 1; q <= c; q++) put(parts[q]) }
function emit() { printf "%s<redacted>", substr(buf, pos, cs - pos); pos = ce + 1 }
BEGIN { n = split("@NAMES@", names, " "); m = 0; maxl = 0
  for (i = 1; i <= n; i++) add(ENVIRON[names[i]])
  for (k in ENVIRON) if (ENVIRON[k] ~ /^postgres(ql)?(\+[a-z0-9]+)?:\/\//) add(ENVIRON[k])
  for (a = 1; a <= m; a++) if (length(secrets[a]) > maxl) maxl = length(secrets[a])
  trunc = (ENVIRON["REDACT_TRUNCATED"] == "1") }
{ L[NR] = $0 }
END { if (NR == 0) exit
  for (c = NR; c > 1; c = int((c + 1) / 2)) { h = int(c / 2)
    for (i = 1; i <= h; i++) L[i] = L[2 * i - 1] "\n" L[2 * i]
    if (c % 2) L[h + 1] = L[c] }
  buf = L[1]; N = length(buf); open = 0; pos = 1; B = (maxl > 4096 ? maxl : 4096)
  for (base = 0; base < N && m > 0; base += B) {
    win = substr(buf, base + 1, B + maxl - 1); cnt = 0; split("", mark)
    for (j = 1; j <= m; j++) { v = secrets[j]; lv = length(v); rest = win; off = 0
      while ((p = index(rest, v)) > 0) { off += p; if (off > B) break
        s = base + off; e = s + lv - 1; if (!(s in mark) || mark[s] < e) mark[s] = e; cnt++
        rest = substr(rest, p + 1) } }
    if (cnt) for (i = base + 1; i <= base + B; i++) if (i in mark) { e = mark[i]
      if (open && i <= ce + 1) { if (e > ce) ce = e } else { if (open) emit(); open = 1; cs = i; ce = e } } }
  if (trunc) { kmax = 0
    for (j = 1; j <= m; j++) { v = secrets[j]; k = length(v) - 1; if (k > N) k = N
      for (; k > kmax; k--) if (substr(buf, N - k + 1) == substr(v, 1, k)) { kmax = k; break } }
    if (kmax > 0) { i = N - kmax + 1
      if (open && i <= ce + 1) ce = N; else { if (open) emit(); open = 1; cs = i; ce = N } } }
  if (open) emit()
  printf "%s\n", substr(buf, pos) }'; }""".replace('@NAMES@', _DEV_REDACT_NAMES)
_DEV_DB_TABLES_TEMPLATE = (
    _DEV_REDACT_FUNCTION
    + r"""
query() { psql -X -At -F '|' -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1" 2>&1; }
tables=$(query "@TABLES@") || { printf '%s\n' "$tables" | redact; exit 1; }
printf '%s\n' "$tables" | redact
case "
$tables
" in *"
@VERSION_TABLE@|t
"*) version=$(query "@VERSION@") || { printf '%s\n' "$version" | redact; exit 1; }
printf '%s\n' "$version" | redact;; esac"""
)
DEV_DB_TABLES_SCRIPT = (
    _DEV_DB_TABLES_TEMPLATE.replace('@TABLES@', _DEV_DB_TABLES_QUERY)
    .replace('@VERSION_TABLE@', DEV_DB_VERSION_TABLE)
    .replace('@VERSION@', _DEV_DB_VERSION_QUERY)
)
DEV_DB_TABLES_TAIL = ('sh', '-c', DEV_DB_TABLES_SCRIPT)

# migrate_status (DEV or TEST mode): alembic's `current`, then `history`, run in the RUNNING data_store container by
# its own interpreter environment (PATH, DATABASE_URI) and its mounted alembic.ini and migrations/;
# no env file and no compose file is read. Read-only against the database: `current` reads
# alembic_version (dont_mutate) and `history` reads the revision files. Nothing is interpolated: the
# two subcommands are literals, so no upgrade, stamp or check is constructible. alembic's output can
# echo an environment value on error, so every printed line, errors included, goes through redact; the
# first non-zero exit status is the script's. A header names each section.
DEV_MIGRATE_STATUS_SCRIPT = (
    _DEV_REDACT_FUNCTION
    + r"""
for sub in current history; do
printf '== alembic %s ==\n' "$sub"
out=$(/code/.venv/bin/alembic "$sub" 2>&1); rc=$?
printf '%s\n' "$out" | redact
[ "$rc" -eq 0 ] || exit "$rc"
done"""
)
DEV_MIGRATE_STATUS_TAIL = ('sh', '-c', DEV_MIGRATE_STATUS_SCRIPT)

# http_get: ONE GET of one of three fixed paths, by exec in the RUNNING data_store container
# against http://127.0.0.1:$APP_INTERNAL_PORT, the way that service's own healthcheck probes /ping (the
# image has python, no curl). The only caller input is NAME, a key of DEV_HTTP_GET_PATHS; the path is a
# literal baked into that name's script, so no caller string reaches the argv or the script. The paths
# are the app_endpoints constants (routers/common PING; routers/data_store GET_UI_CONFIG,
# GET_UI_USAGE_SUMMARY), restated here because this server cannot import them.
DEV_HTTP_GET_PATHS: dict[str, str] = {
    'ping': '/ping',
    'ui_config': '/ui/v1/config',
    'usage_summary': '/ui/v1/stats/usage/summary',
}
# At most this many body bytes are handed on; one more is read to learn the body was longer. The bound
# is the memory, time and scrub-cost bound (the redact awk is super-linear in merged spans).
DEV_HTTP_GET_BODY_LIMIT = 65536
# GET only (http.client, no redirects followed). Accept is the only header the script sets; http.client
# adds Host and Accept-Encoding: identity itself; nothing else is sent, no X-Instance-Secret, nothing
# from the caller. 10 s per socket operation, 20 s overall (a SIGALRM handler raises TimeoutError, so a
# body trickled one byte at a time ends in the transport-failure path). Prints `@@<status>@@<cut>` then
# up to LIMIT body bytes; the cut flag is the result of a length test on the bytes read (more than
# LIMIT), never read from the body. A transport failure prints `@@000@@0` and the error text; the
# except clears the alarm first, so a socket timeout just before the deadline cannot let SIGALRM fire
# inside the handler. Exit: 0 for 2xx, 1 for any other status, 2 for a transport failure. The sh runs it
# as `python -I` with its stderr discarded. No single quote may appear: it is embedded in single
# quotes in the sh below.
_DEV_HTTP_GET_PYTHON = r"""import http.client, os, signal, sys
LIMIT = @LIMIT@
def deadline(signum, frame):
    raise TimeoutError("overall deadline of 20 s exceeded")
signal.signal(signal.SIGALRM, deadline)
signal.alarm(20)
try:
    conn = http.client.HTTPConnection("127.0.0.1", int(os.environ["APP_INTERNAL_PORT"]), timeout=10)
    conn.request("GET", sys.argv[1], headers={"Accept": "application/json"})
    resp = conn.getresponse()
    body = resp.read(LIMIT + 1)
    status = resp.status
    signal.alarm(0)
except Exception as exc:
    signal.alarm(0)
    sys.stdout.write("@@000@@0\n" + type(exc).__name__ + ": " + str(exc) + "\n")
    sys.exit(2)
cut = len(body) > LIMIT
sys.stdout.write("@@%03d@@%d\n" % (status, 1 if cut else 0))
sys.stdout.flush()
sys.stdout.buffer.write(body[:LIMIT])
sys.exit(0 if 200 <= status < 300 else 1)""".replace('@LIMIT@', str(DEV_HTTP_GET_BODY_LIMIT))
# The sh around it. The header is validated against a fixed pattern before it is believed; the body
# and any error text are printed only through redact (embedded verbatim), with REDACT_TRUNCATED=1 (the
# literal, a signal) exported exactly when the python reported the cut. The container's own copy of
# that variable is unset first. python runs with -I (the container's PYTHONPATH holds an agent-writable
# directory, so a planted package could shadow the standard library) and its stderr goes to /dev/null:
# only its stdout, which the except fills before any body, reaches redact. The script never reads
# INSTANCE_WRITE_SECRET itself: only redact does. Two rules read the exit status rc alone, never the
# body. (1) No valid header: a python that exits 0 or 1 did not report an HTTP status (1 is a
# traceback, e.g. the alarm landing inside the except), so both report rc 2; rc above 2 keeps its value
# and a `python exited <rc>` line. (2) A valid header and rc other than 0 or 1: python was killed
# mid-write, so the body may end inside a secret; REDACT_TRUNCATED=1 is set for redact and `python
# exited <rc>, body incomplete` follows the body (after the cut line, when that printed too).
_DEV_HTTP_GET_TEMPLATE = (
    _DEV_REDACT_FUNCTION
    + r"""
GET_PATH=@PATH@
unset REDACT_TRUNCATED
cut=
res=$(/code/.venv/bin/python -I -c '@PYTHON@' "$GET_PATH" 2>/dev/null); rc=$?
hdr=${res%%
*}
body=${res#*
}
[ "$res" = "$hdr" ] && body=
status=
case "$hdr" in
@@000@@0) ;;
@@[0-9][0-9][0-9]@@0) status=${hdr#@@}; status=${status%@@0};;
@@[0-9][0-9][0-9]@@1) status=${hdr#@@}; status=${status%@@1}; cut=1; export REDACT_TRUNCATED=1;;
esac
if [ -z "$status" ]; then
[ "$hdr" != '@@000@@0' ] || res=$body
printf '== GET %s: no HTTP response ==\n' "$GET_PATH"
printf '%s\n' "$res" | redact
case "$rc" in 0|1|2) ;; *) printf 'python exited %s\n' "$rc";; esac
[ "$rc" -gt 1 ] || rc=2
exit "$rc"
fi
printf '== GET %s -> HTTP %s ==\n' "$GET_PATH" "$status"
case "$rc" in 0|1) ;; *) export REDACT_TRUNCATED=1;; esac
printf '%s\n' "$body" | redact
[ -z "$cut" ] || printf '== body cut at @LIMIT@ bytes ==\n'
case "$rc" in 0|1) ;; *) printf 'python exited %s, body incomplete\n' "$rc";; esac
exit $rc"""
)


def _dev_http_get_script(path: str) -> str:
    return (
        _DEV_HTTP_GET_TEMPLATE.replace('@PYTHON@', _DEV_HTTP_GET_PYTHON)
        .replace('@PATH@', path)
        .replace('@LIMIT@', str(DEV_HTTP_GET_BODY_LIMIT))
    )


# name -> its whole tail. Each is a literal row of DEV_EXEC_COMMANDS below.
DEV_HTTP_GET_TAILS: dict[str, tuple[str, ...]] = {
    name: ('sh', '-c', _dev_http_get_script(path)) for name, path in DEV_HTTP_GET_PATHS.items()
}

# THE TABLE. Each whole tail an exec may run (program, flag and script) maps to the ONE option set it
# runs with. A tail not listed is refused even when its program is `sh`; a listed tail with any other
# option set is refused.
DEV_EXEC_COMMANDS: dict[tuple[str, ...], tuple[str, ...]] = {
    DEV_DB_TABLES_TAIL: _DEV_EXEC_OPTIONS_POSTGRES,
    DEV_MIGRATE_STATUS_TAIL: _DEV_EXEC_OPTIONS_CODE,
    **dict.fromkeys(DEV_HTTP_GET_TAILS.values(), ()),
}


def dev_lookup_argv(service: str) -> tuple[str, ...]:
    """`docker ps` for the running, non-one-off containers of DEV_PROJECT's SERVICE; prints ids only."""
    return (
        DOCKER,
        'ps',
        '--filter',
        'status=running',
        '--filter',
        f'label={DEV_LABEL_PROJECT}={DEV_PROJECT}',
        '--filter',
        f'label={DEV_LABEL_SERVICE}={service}',
        '--filter',
        f'label={DEV_LABEL_ONEOFF}=False',
        '--format',
        '{{.ID}}',
    )


def check_dev_lookup_argv(argv: Sequence[str]) -> None:
    """Refuse any lookup argv but dev_lookup_argv() of one DEV_SERVICES member, word for word."""
    words = tuple(argv)
    if not any(words == dev_lookup_argv(service) for service in DEV_SERVICES):
        raise Refused('a dev lookup is `docker ps` filtered to one dev service of the project, printing ids only')


def dev_lookup_step(stack_dir: Path, service: str) -> Step:
    """The label lookup of one dev service's running container(s), checked as built."""
    argv = dev_lookup_argv(validate_dev_service(service))
    check_dev_lookup_argv(argv)
    return Step(argv, stack_dir)


def check_container_id(value: object) -> str:
    """A container id as `docker ps --format {{.ID}}` prints it: lowercase hex, 12 to 64 digits."""
    if not isinstance(value, str) or not _CONTAINER_ID.fullmatch(value):
        raise Refused('the container lookup returned something that is not a container id')
    return value


def pick_dev_target(service: str, lookup_stdout: bytes) -> str:
    """The one running container the lookup found; Refused on none or several, naming the count.

    Refusing on several is deliberate: guessing between two containers of one service would exec into
    one the user did not mean. The ids are never listed.
    """
    ids = [line.strip() for line in lookup_stdout.decode('utf-8', errors='replace').splitlines() if line.strip()]
    if not ids:
        raise Refused(
            f'no running {service} container in the {DEV_PROJECT} project: '
            'start the project first: up (your dev data) or test_up (test mode)'
        )
    if len(ids) > 1:
        raise Refused(
            f'{len(ids)} running {service} containers in the {DEV_PROJECT} project; '
            'refusing to guess which one to exec into'
        )
    return check_container_id(ids[0])


def optional_dev_target(service: str, lookup_stdout: bytes) -> str | None:
    """The one running container the lookup found, None when SERVICE is not running; Refused on several."""
    refuse_several_dev_containers(service, lookup_stdout)
    ids = [line.strip() for line in lookup_stdout.decode('utf-8', errors='replace').splitlines() if line.strip()]
    return check_container_id(ids[0]) if ids else None


def check_dev_exec_argv(argv: Sequence[str]) -> None:
    """Refuse any argv but `docker exec <options> <id> <tail>` for one row of DEV_EXEC_COMMANDS, whole.

    Checked on the words that go to the daemon, not on the builders' intent. The tail must be wholly a
    key of the table and the words between `exec` and the container id must be exactly that key's one
    option set, in order: no other option, no other value, no other combination, no attached spelling.
    The id is a container id as the lookup prints it.
    """
    words = tuple(argv)
    if words[:2] != (DOCKER, 'exec'):
        raise Refused('a dev exec runs only `docker exec`')
    for tail, options in DEV_EXEC_COMMANDS.items():
        head = (DOCKER, 'exec', *options)
        if words[: len(head)] == head and len(words) == len(head) + 1 + len(tail) and words[len(head) + 1 :] == tail:
            check_container_id(words[len(head)])
            return
    raise Refused('a dev exec runs only its fixed programs with their fixed arguments and options')


def dev_exec_steps(stack_dir: Path, container_id: str, tail: Sequence[str]) -> list[Step]:
    """One `docker exec` Step: the checked id, a tail from DEV_EXEC_COMMANDS and that tail's own options.

    The options come from the table and are not arguments: a caller cannot choose a user, a working
    directory or the read-only variable. Checked as built.
    """
    options = DEV_EXEC_COMMANDS.get(tuple(tail))
    if options is None:
        raise Refused('a dev exec runs only its fixed programs with their fixed arguments and options')
    argv = (DOCKER, 'exec', *options, check_container_id(container_id), *tail)
    check_dev_exec_argv(argv)
    return [Step(argv, stack_dir)]


def dev_db_tables_steps(stack_dir: Path, container_id: str) -> list[Step]:
    """db_tables' one exec: the fixed psql script, as user postgres, in a read-only session."""
    return dev_exec_steps(stack_dir, container_id, DEV_DB_TABLES_TAIL)


def dev_migrate_status_steps(stack_dir: Path, container_id: str) -> list[Step]:
    """migrate_status' one exec: alembic current and history in the running data_store, in /code."""
    return dev_exec_steps(stack_dir, container_id, DEV_MIGRATE_STATUS_TAIL)


def validate_dev_http_get_name(name: object) -> str:
    """NAME must be a key of the closed DEV_HTTP_GET_PATHS; anything else (a path, a URL, empty) is Refused."""
    if not isinstance(name, str) or name not in DEV_HTTP_GET_PATHS:
        raise Refused(f'name must be one of: {", ".join(DEV_HTTP_GET_PATHS)}')
    return name


def dev_http_get_steps(stack_dir: Path, container_id: str, name: str) -> list[Step]:
    """http_get's one exec: the fixed GET script of NAME's path, in the running data_store."""
    return dev_exec_steps(stack_dir, container_id, DEV_HTTP_GET_TAILS[validate_dev_http_get_name(name)])


# The status the script reports on its FIRST line (never a later one: the body follows it and is not
# trusted): `== GET <path> -> HTTP <status> ==`.
_HTTP_GET_STATUS = re.compile(r'== GET \S+ -> HTTP (\d{3}) ==(?:\n|\Z)')


def parse_http_get_status(stdout: bytes) -> int | None:
    """The HTTP status the script printed, or None when it reported no HTTP response."""
    match = _HTTP_GET_STATUS.match(stdout.decode('utf-8', errors='replace'))
    return int(match.group(1)) if match else None


# db_tables' output, parsed. A line is `<table>|t` or `<table>|f` for a DEV_DB_TABLES name, or
# `version_num|<revision>`; anything else is not reported as data (the raw, redacted output stays in
# the step). A revision is alembic's: letters, digits and underscores.
_REVISION = re.compile(r'^[A-Za-z0-9_]{1,64}$')


def parse_db_tables(stdout: bytes) -> tuple[dict[str, bool], str | None]:
    """({table: present}, revision or None) from the script's stdout; unknown lines are ignored."""
    tables: dict[str, bool] = {}
    version: str | None = None
    for line in stdout.decode('utf-8', errors='replace').splitlines():
        name, _, value = line.partition('|')
        if name in DEV_DB_TABLES and value in ('t', 'f'):
            tables[name] = value == 't'
        elif name == 'version_num' and _REVISION.fullmatch(value):
            version = value
    return tables, version
