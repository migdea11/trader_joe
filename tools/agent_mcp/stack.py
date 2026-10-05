"""The agent stack's constants, argument validation, the snapshot, generated env files and command builders.

Nothing here runs Docker. The only subprocesses in this module are two read-only git plumbing calls,
both through run_git(): `git worktree list --porcelain` and `git cat-file blob HEAD:<path>`. Every
docker command is built here as an argument list and run by runner.py.

THE DAEMON ONLY EVER SEES MCP-OWNED INPUTS (ADR tj-4rr0la addendum 5). The Docker daemon is root on
the host and resolves bind-mount sources and the build context there; BuildKit reads the Dockerfile.
A check on an agent-writable tree is always a race, so neither is ever pointed at one:
    THE SNAPSHOT. Before a verb builds or creates a container from a worktree, refresh_snapshot()
        copies the worktree's SNAPSHOT_SOURCES into <stack dir>/source, refusing any symlink or
        special file, and --project-directory is ALWAYS that copy (compose_prefix takes no
        worktree). The build context and every relative bind source are then MCP-owned, and the
        checks on them (check_mount_sources, resolve_test_paths, the migrations guard) are stable.
    THE TRUSTED DOCKERFILE. The agent-stack overlay sets build.dockerfile to TRUSTED_DOCKERFILE, the
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
import secrets
import shutil
import stat
import subprocess  # nosec B404 -- argument lists only, never a shell; see run_git
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from tools.agent_mcp.seeds import MAX_BUNDLE_BYTES
from tools.agent_mcp.settings import Settings


class Refused(Exception):
    """A verb refuses: raised before any docker subprocess runs."""


# ---------------------------------------------------------------------------------------------
# THE STACK. These two mirror the Makefile's AGENT_STACK_PROJECT and AGENT_STACK_COMPOSE exactly --
# the project name and the -f file names in the same order (base, test client, the agent-stack
# overlay, then the fake-mode overlay AFTER it; ADR tj-4rr0la addendum 3 (3), tj-vhboky.61). A
# validator test pins that they agree (tj-c4mosr.5).
PROJECT = 'trader_joe_agent_stack'
COMPOSE_FILES = (
    'docker-compose.yaml',
    'docker-compose.test-client.yaml',
    'docker-compose.agent-stack.yaml',
    'docker-compose.fake.yaml',
)

# Where the MCP image carries its OWN copies of COMPOSE_FILES (tools/agent_mcp/Dockerfile). Never the
# worktree's: a worktree is agent-writable, and compose content from it could mount any host path,
# add privileged: or the Docker socket -- raw compose pass-through, which ADR tj-4rr0la rejects (C).
# --project-directory points relative paths (build contexts, bind mounts) at the snapshot instead.
TRUSTED_COMPOSE_DIR = Path('/opt/agent_mcp/compose')
# The MCP image's own copy of the repository's Dockerfile, which docker-compose.agent-stack.yaml names
# as build.dockerfile for every service it builds (ADR tj-4rr0la addendum 5, ruling 2). A Dockerfile
# change reaches the agent stack when the MCP image is rebuilt, as a compose change does.
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
)

DOCKER = '/usr/local/bin/docker'
GIT = '/usr/bin/git'
# Every docker subprocess reaches the daemon through the socket proxy, never a socket (ADR tj-4rr0la
# addendum 1 (b)).
DOCKER_HOST = 'tcp://socket_proxy:2375'

SERVICES = ('postgres', 'kafka', 'data_store', 'data_ingest')
# Built by stack_up; test_client is rebuilt by run_system_tests' own `run --build` as well. Exactly
# the services with a build: key in COMPOSE_FILES, so builds() also reads it: a `run` of one of them
# builds its image when it is missing.
BUILT_SERVICES = ('data_store', 'data_ingest', 'test_client')
# The long-running SERVICES with a snapshot bind: exactly those whose merged model under
# COMPOSE_FILES has a bind mount with a relative source, resolved in the SNAPSHOT (data_store's
# alembic.ini and migrations, data_ingest's tests/fakes in fake mode). refresh_snapshot() swaps the
# snapshot by rename and removes the old generation, so a running container's binds point at a
# removed directory after any refresh, and a plain `up` recreates only on an image or config
# change. stack_up therefore force-recreates these on every call, so the long-running services run
# the code of the LAST stack_up (tj-zgq5v2). A validator test pins the equality by parsing those
# files, so a new bind into a long-running service without an entry goes red.
SNAPSHOT_BOUND_SERVICES = ('data_store', 'data_ingest')
TAIL_DEFAULT = 200
TAIL_MAX = 2000
MAX_TEST_PATHS = 50
# The whole stack's --wait budget, as prod-launch's: kafka alone declares a 90s start_period.
WAIT_TIMEOUT_SECONDS = 300

# THE SNAPSHOT (ADR tj-4rr0la addendum 5, ruling 1): what refresh_snapshot() copies from a worktree
# into <stack dir>/SOURCE_DIR_NAME, the compose project directory of every verb. EXACTLY the trusted
# Dockerfile's COPY sources from the build context (pyproject.toml, uv.lock, entrypoint.sh, common,
# routers, schemas, gen/proto/python -- the committed generated gRPC code, decision tj-3mk3u5.42 F1 --
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
SNAPSHOT_SOURCES = (
    'pyproject.toml',
    'uv.lock',
    'entrypoint.sh',
    'common',
    'routers',
    'schemas',
    'gen/proto/python',
    'data/store/app',
    'data/ingest/app',
    'data/store/alembic.ini',
    'data/store/migrations',
    'data/store/seeds',
    'tests/system',
    'tests/fakes',
    'pytest.ini',
)
# The copy's caps: generous (the sources are a few MB today), there to stop a worktree that grew a
# dataset or a runaway generator from filling the host disk.
SNAPSHOT_MAX_BYTES = 200 * 1024 * 1024
SNAPSHOT_MAX_FILES = 20_000
SYSTEM_TESTS_DIR = 'tests/system'
MIGRATION_VERSIONS_DIR = 'data/store/migrations/versions'

# ---------------------------------------------------------------------------------------------
# THE STACK DIRECTORY (settings.stack_dir; ADR tj-4rr0la addenda 1 (d) and 2).
ROOT_ENV_NAME = 'agent_stack.env'
STORE_ENV_NAME = 'agent_stack_store.env'
INGEST_ENV_NAME = 'agent_stack_ingest.env'
DATA_DIR_NAME = 'data'
AUDIT_LOG_NAME = 'audit.log'
# The worktree NAME the last stack_up used, re-resolved on every read.
STATE_FILE_NAME = 'stack_worktree'

# The committed defaults each generated file starts from, read from the main checkout's HEAD with
# git cat-file -- never a working-tree file, so no live env file is ever opened.
ENV_DEFAULT_SOURCES = {'root': '.env.default', 'store': 'data/store/.env.default', 'ingest': 'data/ingest/.env.default'}
ENV_FILE_NAMES = {'root': ROOT_ENV_NAME, 'store': STORE_ENV_NAME, 'ingest': INGEST_ENV_NAME}
ENV_FILE_VARIABLES = {'root': 'ROOT_ENV_FILE', 'store': 'STORE_ENV_FILE', 'ingest': 'INGEST_ENV_FILE'}

# The agent stack's own names, each distinct from the user's (.env.default: db, kafka,
# trader_joe_store_api).
AGENT_DATABASE_NAME = 'trader_joe_agent_stack_postgres'
AGENT_BROKER_NAME = 'trader_joe_agent_stack_kafka'
AGENT_STORE_API_NETWORK = 'trader_joe_agent_stack_store_api'

# Set in the ROOT generated file only (tj-c4mosr.3, F1 note). env_file order is root then service
# file, so a service-file value would silently win in the container while compose interpolated the
# root one: DATABASE_NAME and BROKER_NAME would name one host in container_name and another in the
# apps' environment. The secrets and paths follow the same rule so each has one value in one file.
ROOT_ONLY_VARIABLES = (
    'DATABASE_NAME',
    'BROKER_NAME',
    'STORE_API_NETWORK',
    'DATA_DIR',
    'POSTGRES_PASS',
    'INSTANCE_WRITE_SECRET',
    'ROOT_ENV_FILE',
    'STORE_ENV_FILE',
    'INGEST_ENV_FILE',
)
BROKER_CREDENTIALS = ('ALPACA_API_KEY', 'ALPACA_API_SECRET')
# Keys that steer compose or the docker CLI rather than the stack: compose reads COMPOSE_* (the file
# list, the project name, the profiles) from the --env-file itself. The committed defaults come from a
# HEAD agents can move, so such a key is REFUSED, at generation and by guard 2 -- never dropped, which
# would hide a committed change that tries to steer compose.
STEERING_PREFIXES = ('COMPOSE_', 'DOCKER_')

_ENV_LINE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)$')
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


def all_worktree_paths(repo_root: Path) -> list[Path]:
    """Every worktree path git knows, usable or not, resolved -- for the under-no-worktree check."""
    entries = parse_worktree_porcelain(run_git(repo_root, ['worktree', 'list', '--porcelain']))
    return [Path(os.path.realpath(path)) for path, _ in entries] + [repo_root]


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


def validate_service(service: object) -> str:
    """Service must be one of the closed set SERVICES."""
    if not isinstance(service, str) or service not in SERVICES:
        raise Refused(f'service must be one of: {", ".join(SERVICES)}')
    return service


def clamp_tail(tail: object) -> int:
    """An integer, clamped to 1..TAIL_MAX."""
    if isinstance(tail, bool) or not isinstance(tail, int):
        raise Refused('tail must be an integer')
    return max(1, min(tail, TAIL_MAX))


# ---------------------------------------------------------------------------------------------
# THE GENERATED ENV FILES (tj-c4mosr.3 item 3 as amended; ADR tj-4rr0la addendum 2).
def parse_env_text(text: str) -> dict[str, str]:
    """KEY=VALUE lines, in order; comments, blanks and anything else are dropped. Values verbatim."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _ENV_LINE.match(line.strip())
        if match:
            values[match.group(1)] = match.group(2)
    return values


def env_line_keys(text: str) -> list[str]:
    """Every key a line of TEXT could set, in the looser spellings compose's own parser also takes.

    parse_env_text() keeps only strict KEY=VALUE lines; compose also reads `export KEY=VALUE`,
    `KEY = VALUE` and `KEY: VALUE`. Guard 2's steering-key check reads keys through this, so a
    spelling parse_env_text drops cannot slip a COMPOSE_ key past it.
    """
    keys = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        stripped = re.sub(r'^export\s+', '', stripped)
        keys.append(re.split(r'[=:]', stripped, maxsplit=1)[0].strip())
    return keys


def render_env(values: Mapping[str, str]) -> str:
    """Render an env file. Refuses a value that would break the one-line-per-variable format."""
    lines = [
        '# GENERATED by the agent-stack MCP (tools/agent_mcp, ADR tj-4rr0la addendum 2). The agent',
        "# stack's own env: never the user's. Deleting the stack directory regenerates it.",
    ]
    for key, value in values.items():
        if '\n' in value or '\r' in value:
            raise Refused(f'{key} holds a line break; refusing to write it')
        lines.append(f'{key}={value}')
    return '\n'.join(lines) + '\n'


def refuse_steering_keys(source: str, values: Mapping[str, str]) -> None:
    """Refuse when SOURCE sets a key that steers compose or the docker CLI (STEERING_PREFIXES)."""
    steering = sorted(key for key in values if key.startswith(STEERING_PREFIXES))
    if steering:
        raise Refused(f'{source} sets {", ".join(steering)}, which would steer compose; refusing')


def read_committed_defaults(repo_root: Path) -> dict[str, dict[str, str]]:
    """The three committed .env.default files, read from the main checkout's HEAD commit."""
    return {
        kind: parse_env_text(run_git(repo_root, ['cat-file', 'blob', f'HEAD:{source}']))
        for kind, source in ENV_DEFAULT_SOURCES.items()
    }


def env_file_paths(stack_dir: Path) -> dict[str, Path]:
    """The three generated env files' absolute paths, by kind."""
    return {kind: stack_dir / name for kind, name in ENV_FILE_NAMES.items()}


def build_env_values(stack_dir: Path, defaults: Mapping[str, Mapping[str, str]]) -> dict[str, dict[str, str]]:
    """The three files' contents: the committed defaults with the agent stack's overrides.

    Root: every committed root variable, then random POSTGRES_PASS and INSTANCE_WRITE_SECRET, the
    agent stack's DATABASE_NAME, BROKER_NAME, STORE_API_NETWORK and DATA_DIR, and the three
    *_ENV_FILE paths. Store and ingest: their committed variables, less every ROOT_ONLY_VARIABLES
    name. ALPACA_API_KEY and ALPACA_API_SECRET present and EMPTY in the ingest file only.

    Refuses when any committed default's key starts with one of STEERING_PREFIXES.
    """
    for kind, values in defaults.items():
        refuse_steering_keys(ENV_DEFAULT_SOURCES.get(kind, kind), values)
    paths = env_file_paths(stack_dir)
    root = {k: v for k, v in defaults['root'].items() if k not in BROKER_CREDENTIALS}
    root.update(
        # Hex, as .env.default asks: these land unencoded in DATABASE_URI.
        POSTGRES_PASS=secrets.token_hex(24),
        INSTANCE_WRITE_SECRET=secrets.token_hex(32),
        DATABASE_NAME=AGENT_DATABASE_NAME,
        BROKER_NAME=AGENT_BROKER_NAME,
        STORE_API_NETWORK=AGENT_STORE_API_NETWORK,
        DATA_DIR=str(stack_dir / DATA_DIR_NAME),
        ROOT_ENV_FILE=str(paths['root']),
        STORE_ENV_FILE=str(paths['store']),
        INGEST_ENV_FILE=str(paths['ingest']),
    )
    dropped = set(ROOT_ONLY_VARIABLES) | set(BROKER_CREDENTIALS)
    store = {k: v for k, v in defaults['store'].items() if k not in dropped}
    ingest = {k: v for k, v in defaults['ingest'].items() if k not in dropped}
    ingest.update(dict.fromkeys(BROKER_CREDENTIALS, ''))
    return {'root': root, 'store': store, 'ingest': ingest}


def _write_private(path: Path, text: str) -> None:
    """Write a 0600 file atomically: a temporary beside it, then a rename over any old one."""
    temporary = path.with_name(f'.{path.name}.tmp')
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        handle.write(text)
    os.replace(temporary, path)


def ensure_env_files(settings: Settings) -> Path:
    """Generate the three env files at first use; afterwards reuse them. Returns the root one.

    The secrets live in the root file only, and it is written LAST, so its presence means the set is
    complete. A missing store or ingest file is rewritten from the committed defaults (it holds no
    secret); the root file is never regenerated while it exists, because Postgres keeps the password
    it was initialised with.
    """
    paths = env_file_paths(settings.stack_dir)
    if paths['root'].exists() and paths['store'].exists() and paths['ingest'].exists():
        return paths['root']
    values = build_env_values(settings.stack_dir, read_committed_defaults(settings.repo_root))
    for kind in ('store', 'ingest'):
        if not paths[kind].exists():
            _write_private(paths[kind], render_env(values[kind]))
    if not paths['root'].exists():
        _write_private(paths['root'], render_env(values['root']))
    return paths['root']


def read_env_file(path: Path) -> dict[str, str]:
    """Read one generated env file. It must be a regular file, not a symlink."""
    if path.is_symlink() or not path.is_file():
        raise Refused(f'{path.name} is missing, or is not a regular file')
    return parse_env_text(path.read_text())


def check_env_file_paths(settings: Settings, worktree_paths: Sequence[Path]) -> Path:
    """GUARD 2 of ADR tj-4rr0la addendum 2 -- runs before ANY docker subprocess of every verb.

    Refuses unless ROOT_ENV_FILE, STORE_ENV_FILE and INGEST_ENV_FILE in the root generated file are
    each an absolute path, free of symlinks, of an existing regular file directly inside the agent
    stack's own directory, and under no worktree (the base file's defaults resolve to the user's
    live env files, and the MCP container can see the main checkout). Also refuses unless
    ROOT_ENV_FILE is the --env-file itself; DATA_DIR is the stack directory's data/; DATABASE_NAME,
    BROKER_NAME and STORE_API_NETWORK carry the agent stack's names and appear in the root file
    only (the F1 rule); ALPACA_API_KEY and ALPACA_API_SECRET are present and empty in the ingest
    file and absent from the other two. And refuses when any of the three files sets a key starting
    COMPOSE_ or DOCKER_ (STEERING_PREFIXES): compose reads COMPOSE_* from the --env-file, so an edited
    file could change the -f list or the project. Returns the root file's path for --env-file.
    """
    paths = env_file_paths(settings.stack_dir)
    root_values = read_env_file(paths['root'])
    for kind, path in paths.items():
        read_env_file(path)  # a regular file, not a symlink, before its raw text is read
        refuse_steering_keys(ENV_FILE_NAMES[kind], dict.fromkeys(env_line_keys(path.read_text()), ''))
    for kind, variable in ENV_FILE_VARIABLES.items():
        raw = root_values.get(variable, '')
        if not raw or not os.path.isabs(raw):
            raise Refused(f'{variable} must be set to an absolute path')
        resolved = Path(os.path.realpath(raw))
        if str(resolved) != raw or resolved.parent != settings.stack_dir or resolved != paths[kind]:
            raise Refused(f"{variable} must name the agent stack's own {ENV_FILE_NAMES[kind]}, with no symlink")
        if any(_within(resolved, worktree) for worktree in worktree_paths):
            raise Refused(f'{variable} lies under a worktree')
        if resolved.is_symlink() or not resolved.is_file():
            raise Refused(f'{variable} does not name a regular file')
    if root_values.get('DATA_DIR') != str(settings.stack_dir / DATA_DIR_NAME):
        raise Refused("DATA_DIR must be the agent stack's own data directory")
    expected_names = {
        'DATABASE_NAME': AGENT_DATABASE_NAME,
        'BROKER_NAME': AGENT_BROKER_NAME,
        'STORE_API_NETWORK': AGENT_STORE_API_NETWORK,
    }
    for variable, expected in expected_names.items():
        if root_values.get(variable) != expected:
            raise Refused(f"{variable} must be the agent stack's own name, {expected}")
    for kind in ('store', 'ingest'):
        values = read_env_file(paths[kind])
        leaked = [name for name in ROOT_ONLY_VARIABLES if name in values]
        if leaked:
            raise Refused(f'{ENV_FILE_NAMES[kind]} sets {", ".join(leaked)}, which belong in the root file only')
    for kind in ('root', 'store'):
        values = root_values if kind == 'root' else read_env_file(paths[kind])
        if any(name in values for name in BROKER_CREDENTIALS):
            raise Refused(f'{ENV_FILE_NAMES[kind]} sets a broker credential variable')
    ingest_values = read_env_file(paths['ingest'])
    if any(ingest_values.get(name, None) != '' for name in BROKER_CREDENTIALS):
        raise Refused(f'{INGEST_ENV_NAME} must carry ALPACA_API_KEY and ALPACA_API_SECRET, empty')
    return paths['root']


# ---------------------------------------------------------------------------------------------
# THE DATA DIRECTORY (stack_wipe; tj-c4mosr.3 item 5).
def check_data_dir(settings: Settings) -> Path | None:
    """The agent stack's data directory, checked; None when it does not exist yet.

    Refuses when the path is a symlink, when resolving it gives anything but the configured absolute
    path (a symlink anywhere above it), or when it is not a directory.
    """
    expected = settings.stack_dir / DATA_DIR_NAME
    if expected.is_symlink():
        raise Refused('the data directory is a symlink; refusing to delete anything')
    if not expected.exists():
        return None
    if Path(os.path.realpath(expected)) != expected or not expected.is_dir():
        raise Refused('the data directory does not resolve to its configured path; refusing to delete anything')
    return expected


def is_plain_dir(path: Path) -> bool:
    """True when PATH is a directory itself, by lstat: a symlink to a directory is not one, nor is a missing path.

    stack_wipe clears only these service mounts (ADR tj-4rr0la addendum 6, INFO): the daemon would
    follow a data/<service> link as root, so a link is left for remove_data_dir to report as 'failed'.
    """
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except FileNotFoundError:
        return False


def remove_data_dir(settings: Settings) -> list[str]:
    """Re-check, then remove the data directory, which the clear steps have emptied. Returns what is left.

    os.rmdir only, never a tree walk: Postgres leaves its data directory owned by its own uid with
    mode 0700, which the server's user cannot open but, owning the parent, can remove once it is
    empty. So each DATA_MOUNTS service directory is removed, then the data directory itself. Anything
    that will not go -- a directory the clear step left non-empty, a symlink, an unexpected entry --
    is returned by its path under the stack directory, the data kept; an empty list means removed
    (or never there). The caller reports a non-empty list as 'failed', never as an error.
    """
    checked = check_data_dir(settings)
    if checked is None:
        return []
    remaining = []
    for service, _ in DATA_MOUNTS:
        entry = checked / service
        if not entry.is_symlink() and not entry.exists():
            continue
        try:
            os.rmdir(entry)  # refuses a symlink (ENOTDIR) and a non-empty directory alike
        except OSError:
            remaining.append(f'{DATA_DIR_NAME}/{service}')
    if remaining:
        return remaining
    try:
        os.rmdir(checked)
    except OSError:
        try:
            left = sorted(os.listdir(checked))
        except OSError:
            left = []
        return [f'{DATA_DIR_NAME}/{name}' for name in left] or [DATA_DIR_NAME]
    return []


# ---------------------------------------------------------------------------------------------
# THE SNAPSHOT (ADR tj-4rr0la addendum 5, ruling 1). PREMISE, named: nothing but the MCP writes under
# the stack directory. settings.load_settings refuses a stack directory inside or containing the
# repository or AGENT_HOME_PATH (the devcontainer mounts both), and the agent stack's containers get
# no writable bind into the snapshot (the overlay mounts data_store's sources :ro).
#
# One snapshot operation at a time, across threads: a verb that timed out leaves its copy thread
# running until it next checks its cancel event, and the next verb's copy must not interleave with it.
_SNAPSHOT_LOCK = threading.Lock()
_O_NOFOLLOW_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
_O_NOFOLLOW_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_COPY_CHUNK = 1024 * 1024


def _make_snapshot_dir(path: Path, *, parents: bool = False) -> None:
    """Create a snapshot directory 0755, whatever the umask.

    World-readable on purpose: the agent stack's containers read the snapshot through bind mounts
    as their own users (appuser, not the MCP's), and BuildKit sends it as the build context. It holds
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


def ensure_snapshot(stack_dir: Path) -> Path:
    """The existing snapshot, or an empty one when there is none. Reads no worktree.

    For the verbs that build and mount nothing from source (stack_down, stack_wipe, logs, ps): compose
    still needs a project directory, and it is always the snapshot.
    """
    live = snapshot_dir(stack_dir)
    with _SNAPSHOT_LOCK:
        if live.is_symlink() or (live.exists() and not live.is_dir()):
            _remove_generation(live)
        if not live.exists():
            _make_snapshot_dir(live)
    return live


# ---------------------------------------------------------------------------------------------
# COMMANDS. Every docker command starts with compose_prefix(): the fixed project, the SNAPSHOT as the
# project directory, the generated --env-file, then the trusted -f files in COMPOSE_FILES order. The
# builders take the stack directory, never a worktree: no compose argv carries a repository path.
@dataclass(frozen=True)
class Step:
    """One subprocess: its argument list and the directory it runs in (the snapshot).

    builds is True for a step that CAN build an image from TRUSTED_DOCKERFILE -- compose builds a
    missing image on up/run without --build (ADR tj-4rr0la addendum 15). _steps() sets it from the
    compose command (builds()), so no builder can add a build without it; the runner makes BASE_IMAGES
    present before the first such step of a verb and never runs it when a base could not be pulled.

    stdout_cap, when set, marks the step's stdout as DATA rather than output (seed_dump's bundle):
    the runner reads it in full up to stdout_cap bytes plus one -- not under the OUTPUT_CAP_BYTES
    tail rule -- and withholds it from the response and the audit line.
    """

    argv: tuple[str, ...]
    cwd: Path
    builds: bool = False
    stdout_cap: int | None = None


def compose_prefix(stack_dir: Path, root_env_file: Path) -> list[str]:
    """The prefix: docker compose -p PROJECT --project-directory <stack_dir>/source --env-file <root> -f <each>."""
    project_directory = str(snapshot_dir(stack_dir))
    prefix = [
        DOCKER,
        'compose',
        '-p',
        PROJECT,
        '--project-directory',
        project_directory,
        '--env-file',
        str(root_env_file),
    ]
    for name in COMPOSE_FILES:
        prefix += ['-f', str(TRUSTED_COMPOSE_DIR / name)]
    return prefix


def builds(tail: Sequence[str]) -> bool:
    """Whether a compose command (the words after compose_prefix) CAN build.

    compose builds a missing image on up/run without --build, so this is: `build`; any `--build`;
    `up`, always (an `up` naming no service builds every one); or a `run` naming a BUILT_SERVICES
    service (postgres and kafka are image-only, so stack_wipe's clear runs stay base-free). A word
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


def _steps(stack_dir: Path, root_env_file: Path, *tails: Sequence[str]) -> list[Step]:
    prefix = compose_prefix(stack_dir, root_env_file)
    return [Step(tuple(prefix + list(tail)), snapshot_dir(stack_dir), builds(tail)) for tail in tails]


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


def stack_up_steps(stack_dir: Path, root_env_file: Path) -> list[Step]:
    """Build the images from the snapshot, start the infrastructure, then force-recreate the app services.

    Three steps. The plain `up` of the services with no snapshot bind (postgres, kafka) recreates
    them only on a config change, so their data and Kafka's start_period are not paid on every call.
    The last `up` force-recreates SNAPSHOT_BOUND_SERVICES so their binds resolve in the snapshot just
    refreshed, and creates each once per stack_up; --no-deps is safe because the step before already
    waited for the infrastructure healthy. Both `up` steps can build (builds()), so the runner's
    BASE_IMAGES check still runs before the first building step (ADR tj-4rr0la addenda 14-15).
    """
    wait = ['-d', '--wait', '--wait-timeout', str(WAIT_TIMEOUT_SECONDS)]
    infrastructure = [service for service in SERVICES if service not in SNAPSHOT_BOUND_SERVICES]
    return _steps(
        stack_dir,
        root_env_file,
        ['build', *BUILT_SERVICES],
        ['up', *wait, *infrastructure],
        ['up', *wait, '--force-recreate', '--no-deps', *SNAPSHOT_BOUND_SERVICES],
    )


def stack_down_steps(stack_dir: Path, root_env_file: Path) -> list[Step]:
    """Stop and remove the agent stack's containers and networks."""
    return _steps(stack_dir, root_env_file, ['down', '--remove-orphans'])


# Each service's data mount target (docker-compose.agent-stack.yaml mounts ${DATA_DIR}/<service>
# there), cleared from inside a one-off container of that service as root: Postgres and Kafka own
# their files, not the MCP's user. The script takes the target as $1; the globs cover dotfiles, and
# rm -f ignores a glob that matched nothing. The target is the container side of a mount the guard
# has checked.
DATA_MOUNTS = (('postgres', '/var/lib/postgresql/data'), ('kafka', '/var/lib/kafka'))
_CLEAR_SCRIPT = 'rm -rf -- "$1"/* "$1"/.[!.]* "$1"/..?*'


def wipe_clear_steps(stack_dir: Path, root_env_file: Path, services: Sequence[str]) -> list[Step]:
    """Clear the named services' data mounts from inside the containers."""
    targets = dict(DATA_MOUNTS)
    return _steps(
        stack_dir,
        root_env_file,
        *(
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
                _CLEAR_SCRIPT,
                'clear',
                targets[service],
            ]
            for service in services
        ),
    )


def postgres_running_steps(stack_dir: Path, root_env_file: Path) -> list[Step]:
    """Run `compose ps -q postgres`: empty output means it is not running."""
    return _steps(stack_dir, root_env_file, ['ps', '-q', 'postgres'])


def alembic_steps(stack_dir: Path, root_env_file: Path, *commands: Sequence[str]) -> list[Step]:
    """One `run --rm --no-deps data_store alembic <command>` per command, as run_migrations.sh does."""
    return _steps(
        stack_dir,
        root_env_file,
        *(['run', '--rm', '--no-deps', 'data_store', '/code/.venv/bin/alembic', *command] for command in commands),
    )


def system_tests_steps(stack_dir: Path, root_env_file: Path, paths: Sequence[str]) -> list[Step]:
    """The test client, rebuilt from the snapshot, run against the agent stack."""
    return _steps(stack_dir, root_env_file, ['run', '--rm', '--no-deps', '--build', 'test_client', *paths])


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


def seed_dump_steps(stack_dir: Path, root_env_file: Path, date: str | None) -> list[Step]:
    """Build test_client from the snapshot, then run the producer in it; its stdout is the bundle.

    The build is a step of its own so the run is the one invocation make seed-dump also uses; both
    steps can build, so the runner makes BASE_IMAGES present before the first (addenda 14-15).
    """
    date_args = ['--date', date] if date is not None else []
    build, run = _steps(stack_dir, root_env_file, ['build', 'test_client'], [*SEED_PRODUCER_RUN, *date_args])
    return [build, replace(run, stdout_cap=MAX_BUNDLE_BYTES)]


def logs_steps(stack_dir: Path, root_env_file: Path, service: str, tail: int) -> list[Step]:
    """The last `tail` lines of one service's log."""
    return _steps(stack_dir, root_env_file, ['logs', '--no-color', '--tail', str(tail), service])


def ps_steps(stack_dir: Path, root_env_file: Path) -> list[Step]:
    """The agent stack's containers, stopped ones included, with their health."""
    return _steps(stack_dir, root_env_file, ['ps', '--all'])
