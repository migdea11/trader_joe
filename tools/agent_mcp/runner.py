"""The verbs: one at a time, each under a timeout, with captured and truncated output and one audit line.

No MCP import here, so the dev venv (which does not install the agent-mcp group) can import and test
all of it; server.py only exposes AgentStack.call over MCP.

OFF THE EVENT LOOP: every blocking step -- the git plumbing (worktree list, the committed defaults),
the snapshot copy, verify and swap, and the data-directory removal -- runs through asyncio.to_thread.
A blocked loop could neither answer 'busy' nor fire a verb's timeout. The lock is taken before the
first await, so it still admits one verb at a time.
"""

import asyncio
import contextlib
import errno
import json
import os
import shlex
import signal
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tools.agent_mcp import seeds, stack
from tools.agent_mcp.settings import Settings


# Per stream, per step. The TAIL is kept -- a failure explains itself at the end -- and the response
# says when, and how much, was cut.
OUTPUT_CAP_BYTES = 16_000

# The whole verb, every step included. A verb that hits it is reported as 'timeout', and the process
# it was waiting on is killed; a killed `compose up` leaves whatever it had started running, which
# stack_down or ps then shows.
VERB_TIMEOUT_SECONDS = {
    'stack_up': 1800,
    'stack_down': 300,
    'stack_wipe': 600,
    # migrate (on whichever mode is up): the mode guard, up to three lookups, one image inspect, in TEST
    # mode two digests and up to two stamp reads, then one one-off alembic start; it builds nothing. The
    # 600 s is the upgrade's. migrate_status is the guard, one lookup and one exec (two alembic starts).
    'migrate': 600,
    'migrate_status': 300,
    'run_system_tests': 1800,
    # run_system_tests' budget, for the same work: the rails (the mode guard, ten or so quick docker reads and
    # two digests), a snapshot, a test_client build that on a cold cache re-syncs the image's dependencies,
    # then the producer's scenario POSTs, the fake ingest behind them and the dump. Under the .mcp.json
    # timeout (1860 s). 30 s was the not_available answer's.
    'seed_dump': 1800,
    'logs': 60,
    'ps': 60,
    # The dev pair's budgets are their agent-stack siblings': the same two compose calls, against a
    # project this server never starts or waits for.
    'dev_logs': 60,
    'dev_ps': 60,
    # One `docker ps` lookup and one psql exec, each seconds.
    'dev_db_tables': 60,
    # One `docker ps` lookup and one GET with a 10 s HTTP timeout; far below the .mcp.json timeout.
    'dev_http_get': 60,
    # The label-only pair: the prod guard's `docker ps`, then one compose down or restart (docker's own
    # 10 s stop grace per container); far below the .mcp.json timeout.
    'dev_down': 300,
    'dev_restart': 300,
    # A plain call, not a job: a cold build is the same uv sync and web build as stack_up's. The
    # .mcp.json timeout (tj-iee6sn.4) outlasts it.
    'dev_build': 1800,
    # A plain call: the down, the network lookup and the up with its own 300 s --wait-timeout. Under the
    # .mcp.json timeout. It never builds (dev_build does), so no build time is in this.
    'dev_up': 600,
    # A plain call: the D7 guard, the budget, the network lookup and one up of pgAdmin (postgres through
    # its depends_on, which waits for healthy). Never builds. Under the .mcp.json timeout.
    'dev_tools': 300,
    # Test mode (tj-iee6sn.24). test_build is dev_build's work on two images (the same uv sync on a cold
    # cache), so its 1800 s; test_up is dev_up's without the network step: the mode guard, up to four
    # digest and stamp reads, the down and one up with its own 300 s --wait-timeout. Both are under the
    # .mcp.json timeout.
    'test_build': 1800,
    'test_up': 600,
}

# The docker CLI's whole environment. Fixed, never inherited: compose reads variables from its own
# environment AHEAD of --env-file, so an inherited DATA_DIR or COMPOSE_FILE would redirect the stack,
# and the container's own settings (the token path among them) have no business in a subprocess.
DOCKER_ENV = {
    'PATH': '/usr/local/bin:/usr/bin:/bin',
    'HOME': '/home/agent_mcp',
    'DOCKER_HOST': stack.DOCKER_HOST,
    'LC_ALL': 'C.UTF-8',
}

_worktree_schema = {
    'type': 'string',
    'description': "'root' for the main checkout, or the name of one of this repository's worktrees",
}

# The verbs' input schemas. Closed: additionalProperties false, and AgentStack.call checks the exact
# key set again, so an unknown keyword is refused rather than dropped.
VERB_SCHEMAS: dict[str, dict[str, Any]] = {
    'stack_up': {'properties': {'worktree': _worktree_schema}, 'required': ['worktree']},
    'stack_down': {'properties': {}, 'required': []},
    'stack_wipe': {'properties': {}, 'required': []},
    'migrate': {'properties': {}, 'required': []},
    'migrate_status': {'properties': {}, 'required': []},
    'run_system_tests': {
        'properties': {
            'worktree': _worktree_schema,
            'paths': {
                'type': 'array',
                'items': {'type': 'string'},
                'maxItems': stack.MAX_TEST_PATHS,
                'description': 'paths under tests/system, relative to the worktree; [] runs the whole suite',
            },
        },
        'required': ['worktree', 'paths'],
    },
    'seed_dump': {
        'properties': {
            'worktree': _worktree_schema,
            'date': {
                'type': 'string',
                'description': "the manifest's UTC date, YYYY-MM-DD (a real calendar date); default today",
            },
        },
        'required': ['worktree'],
    },
    'logs': {
        'properties': {
            'service': {'type': 'string', 'enum': list(stack.SERVICES)},
            'tail': {
                'type': 'integer',
                'description': f'lines from the end, clamped to 1..{stack.TAIL_MAX}; default {stack.TAIL_DEFAULT}',
            },
        },
        'required': ['service'],
    },
    'ps': {'properties': {}, 'required': []},
    # THE USER'S DEV PROJECT, READ-ONLY (tj-kzy7w2). Neither schema has -- and neither verb could use
    # -- a project, a compose file or a command argument: the target is stack.DEV_PROJECT, a
    # constant. `service` is the closed stack.DEV_SERVICES enum and `tail` takes the same clamp as
    # the agent stack's logs, so the pair is no looser than its siblings.
    'dev_logs': {
        'properties': {
            'service': {'type': 'string', 'enum': list(stack.DEV_SERVICES)},
            'tail': {
                'type': 'integer',
                'description': f'lines from the end, clamped to 1..{stack.TAIL_MAX}; default {stack.TAIL_DEFAULT}',
            },
        },
        'required': ['service'],
    },
    'dev_ps': {'properties': {}, 'required': []},
    # Takes nothing, like dev_ps: the container, the program and the SQL are constants in stack.py.
    'dev_db_tables': {'properties': {}, 'required': []},
    # The one agent input is NAME from the closed stack.DEV_HTTP_GET_PATHS: no URL, path or header.
    'dev_http_get': {
        'properties': {
            'name': {
                'type': 'string',
                'enum': list(stack.DEV_HTTP_GET_PATHS),
                'description': 'which fixed GET: '
                + ', '.join(f'{n} ({p})' for n, p in stack.DEV_HTTP_GET_PATHS.items()),
            }
        },
        'required': ['name'],
    },
    # THE USER'S DEV PROJECT, STATE-CHANGING, LABEL-ONLY. dev_down takes nothing; dev_restart's one
    # input is the closed stack.DEV_RESTART_SERVICES enum (postgres is not in it).
    'dev_down': {'properties': {}, 'required': []},
    'dev_restart': {
        'properties': {'service': {'type': 'string', 'enum': list(stack.DEV_RESTART_SERVICES)}},
        'required': ['service'],
    },
    # Takes nothing: the services, the files and the digests are constants and computed values.
    'dev_build': {'properties': {}, 'required': []},
    # Takes nothing: the services, the files, the network and the secret wrapper are constants.
    'dev_up': {'properties': {}, 'required': []},
    # Takes nothing: the service, the files, the network and the secret wrapper are constants, and
    # the pgAdmin credentials are read by compose from the root env file, never by this server.
    'dev_tools': {'properties': {}, 'required': []},
    # Take nothing: the services, the files, the images and the data directory are constants.
    'test_build': {'properties': {}, 'required': []},
    'test_up': {'properties': {}, 'required': []},
}
for _schema in VERB_SCHEMAS.values():
    _schema.update(type='object', additionalProperties=False)

_OPTIONAL_DEFAULTS: dict[str, dict[str, Any]] = {
    'logs': {'tail': stack.TAIL_DEFAULT},
    'dev_logs': {'tail': stack.TAIL_DEFAULT},
}

# The seed producer's exit status for a refusal (python -m data.store.seeds: 0 printed, 3 refused, 1
# failed); seed_dump answers 'refused' for it.
SEED_EXIT_REFUSED = 3

# An unknown verb's name is recorded in the audit line cut to this many characters (json.dumps
# escapes it), with known: false.
AUDIT_VERB_NAME_MAX = 64


@dataclass(frozen=True)
class ProcessResult:
    """What one subprocess did. exit_status is None when it was killed at the timeout."""

    exit_status: int | None
    stdout: bytes
    stderr: bytes


_READ_CHUNK = 64 * 1024

# How long a cancelled step's process group gets to exit on SIGTERM before SIGKILL takes the rest.
_KILL_GRACE_SECONDS = 2.0


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    """Signal a whole process group; one that is already gone is not an error."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, sig)


async def _kill_group(process: asyncio.subprocess.Process) -> None:
    """SIGTERM the step's process group, SIGKILL it after the grace, and reap the leader.

    The SIGKILL goes out even when the leader has exited: a child that ignored SIGTERM, or sits
    blocked on a FIFO, is in the group still and must not outlive the verb.
    """
    pgid = process.pid
    _signal_group(pgid, signal.SIGTERM)
    try:
        await asyncio.wait_for(asyncio.shield(process.wait()), _KILL_GRACE_SECONDS)
    except TimeoutError:
        pass
    finally:
        _signal_group(pgid, signal.SIGKILL)
    await process.wait()


async def _read_capped(stream: asyncio.StreamReader, cap: int) -> bytes:
    """Read a stream to its end, keeping at most cap + 1 bytes: one over says 'over the cap' without the rest."""
    kept = bytearray()
    while chunk := await stream.read(_READ_CHUNK):
        if len(kept) <= cap:
            kept += chunk[: cap + 1 - len(kept)]
    return bytes(kept)


async def run_process(step: stack.Step) -> ProcessResult:
    """Run one argument list -- never a shell -- in DOCKER_ENV, capturing both streams.

    A step's own env (stack.Step.env, the dev lifecycle's pinned variables) is merged over DOCKER_ENV
    for that subprocess alone; it appears in no recorded command and no audit line.

    A step with stdout_cap has its stdout read to the end but kept only up to the cap plus one byte,
    so a runaway producer cannot fill the server's memory.

    Every step runs in its OWN session, hence its own process group. Cancelled (the verb's timeout),
    the whole group is killed (SIGTERM, then SIGKILL after a short grace) and the leader reaped
    before the cancellation goes through: nothing the step started -- a docker client, a command
    blocked on a FIFO, a shell's children -- outlives the verb that started it.
    """
    process = await asyncio.create_subprocess_exec(
        *step.argv,
        cwd=step.cwd,
        env={**DOCKER_ENV, **dict(step.env)},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        if step.stdout_cap is None or process.stdout is None or process.stderr is None:
            stdout, stderr = await process.communicate()
        else:
            stdout, stderr = await asyncio.gather(_read_capped(process.stdout, step.stdout_cap), process.stderr.read())
            await process.wait()
    except asyncio.CancelledError:
        await _kill_group(process)
        raise
    return ProcessResult(process.returncode, stdout, stderr)


def truncate(data: bytes) -> dict[str, Any]:
    """Decode one stream and keep its last OUTPUT_CAP_BYTES, saying when it cut."""
    if len(data) <= OUTPUT_CAP_BYTES:
        return {'text': data.decode('utf-8', errors='replace'), 'truncated': False}
    return {
        'text': data[-OUTPUT_CAP_BYTES:].decode('utf-8', errors='replace'),
        'truncated': True,
        'note': f'truncated: showing the last {OUTPUT_CAP_BYTES} of {len(data)} bytes',
    }


def withhold(data: bytes) -> dict[str, Any]:
    """A data stream's place in the response: its size only, never its content (seed_dump's bundle)."""
    return {'text': '', 'truncated': False, 'note': f'withheld: {len(data)} bytes of data, not output'}


class BasePullFailed(Exception):
    """A BASE_IMAGES ref is absent from the daemon and its pull failed: the verb stops before any build."""

    def __init__(self, ref: str):
        super().__init__(f'base image {ref} is not on the daemon and could not be pulled; nothing was built')
        self.ref = ref


@dataclass
class _Call:
    """One verb call in progress: the steps it ran, and its arguments once validated."""

    run: Callable[[stack.Step], Awaitable[ProcessResult]]
    steps: list[dict[str, Any]] = field(default_factory=list)
    validated: dict[str, Any] | None = None
    last_exit: int | None = None
    bases_present: bool = False
    # A verb's structured result beyond its status and message (seed_dump's files); never content.
    details: dict[str, Any] | None = None

    async def ensure_bases(self, cwd: Path) -> None:
        """Per stack.BASE_IMAGES ref: `docker image inspect`, and `docker pull` only when that fails.

        The DAEMON pulls (ADR tj-4rr0la addendum 14): agent_mcp has no egress, and a build that had
        to resolve a base itself would fetch the registry token from in here. A failed pull raises
        BasePullFailed naming the ref, so the build step after it never runs.
        """
        for ref in stack.BASE_IMAGES:
            if (await self.step(stack.base_inspect_step(ref, cwd))).exit_status == 0:
                continue
            if (await self.step(stack.base_pull_step(ref, cwd))).exit_status != 0:
                raise BasePullFailed(ref)
        self.bases_present = True

    async def step(self, step: stack.Step) -> ProcessResult:
        # Every subprocess passes here, so every build does too: the bases are made present before the
        # first step that builds, once per verb, whichever verb and whichever builder produced it.
        if step.builds and not self.bases_present:
            await self.ensure_bases(step.cwd)
        record: dict[str, Any] = {'command': shlex.join(step.argv), 'exit_status': None}
        self.steps.append(record)
        result = await self.run(step)
        stdout = truncate(result.stdout) if step.stdout_cap is None else withhold(result.stdout)
        record.update(exit_status=result.exit_status, stdout=stdout, stderr=truncate(result.stderr))
        self.last_exit = result.exit_status
        return result

    async def steps_until_failure(self, steps: list[stack.Step]) -> bool:
        for step in steps:
            if (await self.step(step)).exit_status != 0:
                return False
        return True


class AgentStack:
    """The verbs, serialised: a second caller is told 'busy'.

    Ten over the one agent stack, plus the two READ-ONLY dev verbs of ADR tj-4rr0la addendum 18,
    which reach the user's own compose project and can only read it, and the READ-ONLY exec verbs
    dev_db_tables of addendum 22, and the two STATE-CHANGING label-only dev
    verbs dev_down and dev_restart (tj-iee6sn.7), held by the D6/D7 guards, the steering guard and
    the budget.
    """

    def __init__(
        self,
        settings: Settings,
        run: Callable[[stack.Step], Awaitable[ProcessResult]] = run_process,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.settings = settings
        self._run = run
        self._clock = clock
        self._running: str | None = None
        self._handlers: Mapping[str, Callable[..., Awaitable[tuple[str, str]]]] = {
            'stack_up': self._stack_up,
            'stack_down': self._stack_down,
            'stack_wipe': self._stack_wipe,
            'migrate': self._migrate,
            'migrate_status': self._migrate_status,
            'run_system_tests': self._run_system_tests,
            'seed_dump': self._seed_dump,
            'logs': self._logs,
            'ps': self._ps,
            'dev_logs': self._dev_logs,
            'dev_ps': self._dev_ps,
            'dev_db_tables': self._dev_db_tables,
            'dev_http_get': self._dev_http_get,
            'dev_down': self._dev_down,
            'dev_restart': self._dev_restart,
            'dev_build': self._dev_build,
            'dev_up': self._dev_up,
            'dev_tools': self._dev_tools,
            'test_build': self._test_build,
            'test_up': self._test_up,
        }
        # S10: at most stack.DEV_BUDGET_LIMIT state-changing dev verbs per rolling hour, in memory.
        self.dev_budget = stack.DevBudget()

    @property
    def audit_log(self) -> Path:
        return self.settings.stack_dir / stack.AUDIT_LOG_NAME

    def _check_arguments(self, verb: str, arguments: object) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            raise stack.Refused('arguments must be an object')
        schema = VERB_SCHEMAS[verb]
        allowed = set(schema['properties'])
        unknown = sorted(set(arguments) - allowed)
        if unknown:
            raise stack.Refused(f'{verb} takes no argument named {", ".join(map(repr, unknown))}')
        missing = [name for name in schema['required'] if name not in arguments]
        if missing:
            raise stack.Refused(f'{verb} needs {", ".join(missing)}')
        return {**_OPTIONAL_DEFAULTS.get(verb, {}), **arguments}

    async def call(self, verb: str, arguments: object) -> dict[str, Any]:
        """Run one verb. Always returns a result, and always appends one audit line."""
        started = self._clock()
        known = verb in self._handlers
        call = _Call(self._run)
        status, message = 'refused', ''
        if not known:
            message = f'unknown verb; the verbs are: {", ".join(self._handlers)}'
        elif self._running is not None:
            status, message = 'busy', f'busy: {self._running} is running; verbs run one at a time, call again later'
        else:
            # No await between this check and the assignment: the lock is the event loop itself.
            self._running = verb
            try:
                checked = self._check_arguments(verb, arguments)
                async with asyncio.timeout(VERB_TIMEOUT_SECONDS[verb]):
                    status, message = await self._handlers[verb](call, **checked)
            except stack.Refused as refusal:
                status, message = 'refused', str(refusal)
            except BasePullFailed as failure:
                status, message = 'failed', f'{verb} stopped: {failure}'
            except TimeoutError:
                status, message = (
                    'timeout',
                    f'{verb} hit its {VERB_TIMEOUT_SECONDS[verb]}s timeout; its process was killed, or its copy stopped',
                )
            except Exception as error:
                status, message = 'error', f'{type(error).__name__}: {error}'
            finally:
                self._running = None
        result: dict[str, Any] = {
            'verb': verb if known else None,
            'status': status,
            'exit_status': call.last_exit,
            'message': message,
            'steps': call.steps,
            'duration_s': round(self._clock() - started, 3),
            'output_cap_bytes': OUTPUT_CAP_BYTES,
        }
        if call.details is not None:
            result['details'] = call.details
        audit_error = self._audit(result, verb if known else str(verb)[:AUDIT_VERB_NAME_MAX], known, call.validated)
        if audit_error:
            result['audit'] = audit_error
        return result

    def _audit(self, result: Mapping[str, Any], name: str, known: bool, validated: dict[str, Any] | None) -> str | None:
        """One JSON line: UTC time, verb, known, validated arguments, status, exit status, duration.

        No output and no env value. Arguments appear only once a verb has accepted them, so an
        argument that was refused never reaches the log. An unknown verb is recorded by the name it
        was called with, cut to AUDIT_VERB_NAME_MAX characters and JSON-escaped, with known: false
        and arguments null.
        """
        line = json.dumps(
            {
                'time': datetime.now(UTC).isoformat(timespec='seconds'),
                'verb': name,
                'known': known,
                'arguments': validated,
                'status': result['status'],
                'exit_status': result['exit_status'],
                'duration_s': result['duration_s'],
            }
        )
        try:
            descriptor = os.open(self.audit_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, 'a') as handle:
                handle.write(line + '\n')
        except OSError as error:
            return f'the audit log could not be written: {type(error).__name__}'
        return None

    # -----------------------------------------------------------------------------------------
    # SHARED STEPS
    def _guarded_env_blocking(self) -> Path:
        stack.ensure_env_files(self.settings)
        return stack.check_env_file_paths(self.settings, stack.all_worktree_paths(self.settings.repo_root))

    async def _guarded_env(self) -> Path:
        """Generate the env files at first use, then GUARD 2 (stack.check_env_file_paths), off the loop."""
        return await asyncio.to_thread(self._guarded_env_blocking)

    async def _worktrees(self) -> dict[str, Path]:
        """This repository's worktrees by name (git worktree list), off the loop."""
        return await asyncio.to_thread(stack.list_worktrees, self.settings.repo_root)

    async def _refresh_snapshot(self, worktree: Path) -> Path:
        """Copy WORKTREE into the snapshot, off the loop; a timeout tells the copy thread to stop."""
        cancel = threading.Event()
        try:
            return await asyncio.to_thread(stack.refresh_snapshot, self.settings.stack_dir, worktree, cancel)
        except asyncio.CancelledError:
            cancel.set()
            raise

    async def _existing_snapshot(self) -> Path:
        """The snapshot as it stands (an empty one if none), off the loop. Reads no worktree."""
        return await asyncio.to_thread(stack.ensure_snapshot, self.settings.stack_dir)

    def _state_file(self) -> Path:
        return self.settings.stack_dir / stack.STATE_FILE_NAME

    # -----------------------------------------------------------------------------------------
    # THE VERBS. Each returns (status, message); stack.Refused from any of them means no docker
    # subprocess ran after the refusal. A worktree NAME is checked before any git call. The verbs
    # that build or create a container from source refresh the snapshot first and check it, never
    # the worktree; the rest read no worktree at all.
    async def _stack_up(self, call: _Call, worktree: object) -> tuple[str, str]:
        """Snapshot WORKTREE, build the service and test-client images from it and start the agent stack, waiting for healthy.

        Every call force-recreates data_store and data_ingest, so the long-running services run the
        code of the LAST stack_up -- tests/fakes included; postgres is kept.
        """
        name = stack.check_worktree_name(worktree)
        path = stack.resolve_worktree(name, await self._worktrees())
        call.validated = {'worktree': name}
        snapshot = await self._refresh_snapshot(path)
        stack.check_mount_sources(snapshot)
        env_file = await self._guarded_env()
        if stack.check_data_dir(self.settings) is None:
            (self.settings.stack_dir / stack.DATA_DIR_NAME).mkdir(mode=0o700)
        self._state_file().write_text(f'{name}\n')
        if not await call.steps_until_failure(stack.stack_up_steps(self.settings.stack_dir, env_file)):
            return 'failed', 'stack_up failed; see the steps, then logs or ps'
        return 'ok', f'agent stack up from a snapshot of {name}'

    async def _stack_down(self, call: _Call) -> tuple[str, str]:
        """Stop the agent stack and remove its containers and networks. Its data is kept."""
        call.validated = {}
        await self._existing_snapshot()
        env_file = await self._guarded_env()
        if not await call.steps_until_failure(stack.stack_down_steps(self.settings.stack_dir, env_file)):
            return 'failed', 'stack_down failed'
        return 'ok', 'agent stack down'

    async def _stack_wipe(self, call: _Call) -> tuple[str, str]:
        """Stop the agent stack and delete its data directory, and nothing else."""
        data_dir = stack.check_data_dir(self.settings)
        call.validated = {}
        await self._existing_snapshot()
        env_file = await self._guarded_env()
        stack_dir = self.settings.stack_dir
        if not await call.steps_until_failure(stack.stack_down_steps(stack_dir, env_file)):
            return 'failed', 'stack_wipe stopped: the stack did not come down, so nothing was deleted'
        if data_dir is None:
            return 'ok', 'agent stack down; there was no data directory to delete'
        populated = [service for service, _ in stack.DATA_MOUNTS if stack.is_plain_dir(data_dir / service)]
        if populated and not await call.steps_until_failure(stack.wipe_clear_steps(stack_dir, env_file, populated)):
            return 'failed', 'stack_wipe stopped: a service data mount could not be cleared; the directory is kept'
        remaining = await asyncio.to_thread(stack.remove_data_dir, self.settings)
        if remaining:
            return 'failed', f'stack_wipe could not remove {", ".join(remaining)}; what is left is kept'
        return 'ok', 'agent stack down and its data directory deleted'

    # run_system_tests and seed_dump (tj-iee6sn.20) run test_client of the ONE project against TEST mode. The
    # rails, in this order, a refusal at any point running nothing after it and charging nothing: the worktree
    # name and the paths (spelling alone, then git), the mode guard (TEST only), the S6 stale-copy check, the D5
    # env-file preflight, the postgres data-mount check (one lookup, one fixed inspect: it must be the test
    # directory), the source stamp of every running data_store and data_ingest container (the :test images come
    # from the root checkout), then the snapshot of the worktree and its checks, the budget (once), and the steps.
    async def _test_mode_rails(self, call: _Call) -> tuple[str, str] | None:
        """The preconditions of the two system-suite verbs up to the snapshot; Refused at the first that fails, or a failed result when a digest could not be computed."""
        settings = self.settings
        await self._mode_guard(call, stack.MODES_TEST, stack.SYSTEM_SUITE_MODE_HINT)
        await asyncio.to_thread(stack.check_trusted_copies_current, settings.repo_root)
        await asyncio.to_thread(stack.env_file_preflight, settings.repo_root)
        postgres = await self._dev_exec_target(call, 'postgres')
        mount = await call.step(stack.postgres_mount_step(settings.stack_dir, postgres))
        stack.check_test_postgres_mount(mount.exit_status, mount.stdout, settings.repo_host_path)
        lookup = await call.step(stack.dev_lookup_step(settings.stack_dir, 'data_store'))
        if lookup.exit_status != 0:
            raise stack.Refused(f'the container lookup failed (exit {lookup.exit_status}); see the steps')
        if stack.optional_dev_target('data_store', lookup.stdout) is None:
            raise stack.Refused(f'no running data_store container in the {stack.DEV_PROJECT} project: call test_up')
        digests, failed = await self._source_digests(call)
        if failed is not None:
            return 'failed', f'the source digest failed ({failed}); nothing was run'
        return await self._running_stamps_current(call, lookup.stdout, digests)

    async def _run_system_tests(self, call: _Call, worktree: object, paths: object) -> tuple[str, str]:
        """STATE-CHANGING: runs tests/system (or PATHS under it) from test_client against TEST mode, the project test_up started, and WRITES THE TEST DATABASE (./volume_data/test), never your dev data. Needs test_up (and migrate) first. The services run the root checkout's :test images; the client is built from a fresh snapshot of WORKTREE ('root' or a worktree name), so that worktree's tests and code reach the client unintegrated, and a test that exists only there is collected. PATHS are paths under tests/system relative to the worktree ([] runs the whole suite). Runs nothing unless, in order: only test mode is up (a DEV or PROD container, or both modes, is refused, so a dev data_ingest refuses: the fake broker is what makes the suite safe), the postgres data mount is exactly <repo>/volume_data/test/postgres, and every running data_store and data_ingest container carries a source stamp equal to the root checkout's digest (else: stale test stack, call test_build, then test_up). The client reads the instance write secret from the root env file through compose, exactly as data_store does; it is on no command line and never passes through this server. Refused also when the MCP image is stale (run make agent-mcp-rebuild), when an env file is a symlink or not a regular file, and after 12 state-changing dev calls in an hour. Up to 30 minutes; returns ok or failed itself."""
        name = stack.check_worktree_name(worktree)
        checked_paths = stack.check_test_paths(paths)
        call.validated = {'worktree': name, 'paths': checked_paths}
        path = stack.resolve_worktree(name, await self._worktrees())
        rails = await self._test_mode_rails(call)
        if rails is not None:
            return rails
        snapshot = await self._refresh_snapshot(path)
        stack.check_mount_sources(snapshot)
        resolved = stack.resolve_test_paths(snapshot, paths)
        call.validated = {'worktree': name, 'paths': resolved}
        self.dev_budget.charge()
        steps = stack.system_tests_steps(self.settings.stack_dir, self.settings.repo_host_path, resolved)
        if not await call.steps_until_failure(steps):
            return 'failed', f'the system suite failed (pytest exit status {call.last_exit})'
        return 'ok', 'the system suite passed'

    async def _seed_dump(self, call: _Call, worktree: object, date: object = None) -> tuple[str, str]:
        """STATE-CHANGING: runs the seed producer in test_client against TEST mode (up, migrated, the fake broker) and WRITES THE TEST DATABASE (./volume_data/test), never your dev data; then writes <revision>.sql and <revision>.json under agent_mcp_seeds/<worktree>/ in the share directory. Needs test_up (and migrate) first. The services run the root checkout's :test images; the client is built from a fresh snapshot of WORKTREE ('root' or a worktree name). DATE (YYYY-MM-DD, a real calendar date) is the manifest's; default today. Runs nothing unless the same rails as run_system_tests hold, in order: only test mode up, the postgres data mount exactly <repo>/volume_data/test/postgres, every running data_store and data_ingest source stamp equal to the root checkout's digest. The response names the two files RELATIVE to the share directory -- /agent_mcp_share, the same path in the devcontainer -- with their sizes and the manifest's row counts, never their content. Exit 3 from the producer is 'refused'; a bundle the contract refuses, or stdout over its cap, is 'failed' with nothing written. The producer runs per call in test_client, which has no Docker access and no writable mount; this process reads its stdout and writes the files itself, with no-follow writes (ADR tj-4rr0la addendum 10, relocated by addendum 11 R2). Charged to the state-changing budget once per call (12 an hour). Up to 30 minutes."""
        name = stack.check_worktree_name(worktree)
        checked_date = stack.check_seed_date(date)
        path = stack.resolve_worktree(name, await self._worktrees())
        call.validated = {'worktree': name} if checked_date is None else {'worktree': name, 'date': checked_date}
        rails = await self._test_mode_rails(call)
        if rails is not None:
            return rails
        snapshot = await self._refresh_snapshot(path)
        stack.check_mount_sources(snapshot)
        self.dev_budget.charge()
        build, produce = stack.seed_dump_steps(self.settings.stack_dir, self.settings.repo_host_path, checked_date)
        if (await call.step(build)).exit_status != 0:
            return 'failed', 'seed_dump stopped: test_client did not build; see the steps'
        produced = await call.step(produce)
        if produced.exit_status == SEED_EXIT_REFUSED:
            return 'refused', 'the seed producer refused (exit 3); its stderr is in the steps'
        if produced.exit_status != 0:
            return 'failed', f'the seed producer failed (exit {produced.exit_status}); its stderr is in the steps'
        try:
            bundle = seeds.parse_bundle(produced.stdout)
            files = await asyncio.to_thread(seeds.write_seed, self.settings.agent_home, name, bundle)
        except seeds.BundleRefused as refusal:
            return 'failed', f'the seed bundle was refused and nothing was written: {refusal}'
        except OSError as failure:
            code = errno.errorcode.get(failure.errno or 0, 'unknown')
            return 'failed', f'the seed files could not be written: {type(failure).__name__} ({code})'
        call.details = {
            'relative_to': str(self.settings.agent_home),
            'sql': {'path': files.sql, 'bytes': files.sql_bytes},
            'manifest': {'path': files.manifest, 'bytes': files.manifest_bytes},
            'row_counts': files.row_counts,
        }
        return 'ok', f'seed for revision {bundle.revision} written under {seeds.SEEDS_DIR_NAME}/{name}/'

    async def _logs(self, call: _Call, service: object, tail: object) -> tuple[str, str]:
        """The last TAIL lines (clamped) of one agent-stack service's log."""
        checked_service = stack.validate_service(service)
        lines = stack.clamp_tail(tail)
        call.validated = {'service': checked_service, 'tail': lines}
        await self._existing_snapshot()
        env_file = await self._guarded_env()
        steps = stack.logs_steps(self.settings.stack_dir, env_file, checked_service, lines)
        if not await call.steps_until_failure(steps):
            return 'failed', 'compose logs failed'
        return 'ok', f'last {lines} lines of {checked_service}'

    async def _ps(self, call: _Call) -> tuple[str, str]:
        """The agent stack's containers, stopped ones included, and their health."""
        call.validated = {}
        await self._existing_snapshot()
        env_file = await self._guarded_env()
        if not await call.steps_until_failure(stack.ps_steps(self.settings.stack_dir, env_file)):
            return 'failed', 'compose ps failed'
        return 'ok', 'agent stack containers'

    # -----------------------------------------------------------------------------------------
    # THE USER'S OWN DEV PROJECT, READ-ONLY (stack.DEV_PROJECT; ADR tj-4rr0la addendum 18).
    #
    # These two run NO snapshot, NO git and NO generated env file, and that is deliberate rather than
    # an omission. They read no worktree, so there is nothing to snapshot; they pass no --env-file and
    # no bind source, so GUARD 2 (stack.check_env_file_paths, which exists to keep an agent-writable
    # path out of compose's argv) has nothing to check for them. Calling it anyway would generate the
    # agent stack's env files as a side effect and let an unrelated agent-stack fault refuse the one
    # diagnostic the user reaches for when their own stack is broken -- which is the case these verbs
    # were added for. What stands in its place is stack.check_dev_argv(), run on every argv as it is
    # built, and it is strictly narrower: four fixed words, then one of two read-only subcommands.
    async def _dev_ps(self, call: _Call) -> tuple[str, str]:
        """READ-ONLY: the containers of the USER'S OWN dev compose project, stopped ones included, and their health. It starts, stops and changes nothing, and reaches no other project."""
        call.validated = {}
        if not await call.steps_until_failure(stack.dev_ps_steps(self.settings.stack_dir)):
            return 'failed', f'compose ps failed for the {stack.DEV_PROJECT} project'
        return 'ok', f'containers of the {stack.DEV_PROJECT} compose project'

    async def _dev_logs(self, call: _Call, service: object, tail: object) -> tuple[str, str]:
        """READ-ONLY: the last TAIL lines (clamped) of one service's log in the USER'S OWN dev compose project. It starts, stops and changes nothing, and reaches no other project."""
        checked_service = stack.validate_dev_service(service)
        lines = stack.clamp_tail(tail)
        call.validated = {'service': checked_service, 'tail': lines}
        steps = stack.dev_logs_steps(self.settings.stack_dir, checked_service, lines)
        if not await call.steps_until_failure(steps):
            return 'failed', f'compose logs failed for the {stack.DEV_PROJECT} project'
        return 'ok', f'last {lines} lines of {checked_service} in the {stack.DEV_PROJECT} project'

    # THE EXEC VERBS (ADR tj-4rr0la addendum 22). Like the two above they take no snapshot, no git
    # and no env file; unlike them they run `docker exec`, held by stack.check_dev_exec_argv(). The
    # target is never an argument: a label lookup must find exactly one running container, and a
    # refusal there runs no exec.
    async def _dev_exec_target(self, call: _Call, service: str) -> str:
        """The id of the one running container of SERVICE in the dev project; Refused on none or several."""
        lookup = await call.step(stack.dev_lookup_step(self.settings.stack_dir, service))
        if lookup.exit_status != 0:
            raise stack.Refused(f'the container lookup failed (exit {lookup.exit_status}); see the steps')
        return stack.pick_dev_target(service, lookup.stdout)

    async def _dev_db_tables(self, call: _Call) -> tuple[str, str]:
        """READ-ONLY: which of the store's tables exist in the USER'S OWN dev database (dataset_read, dataset_read_daily, dataset_override_log, dataset_deletion, ingest_run, store_dataset_entry, stock_market_activity, alembic_version), and its alembic revision. Works while data_store is down. Runs one fixed psql script by exec in the running dev postgres container as user postgres, in a read-only transaction; takes no argument and prints no credential."""
        call.validated = {}
        target = await self._dev_exec_target(call, 'postgres')
        step = stack.dev_db_tables_steps(self.settings.stack_dir, target)[0]
        result = await call.step(step)
        if result.exit_status != 0:
            return 'failed', 'the table query failed in the dev postgres container; see the steps'
        tables, revision = stack.parse_db_tables(result.stdout)
        call.details = {'tables': tables, 'alembic_revision': revision}
        present = sum(tables.values())
        version = revision or 'none (no alembic_version row)'
        return 'ok', f'{present} of {len(stack.DEV_DB_TABLES)} tables present; alembic revision: {version}'

    async def _dev_http_get(self, call: _Call, name: object) -> tuple[str, str]:
        """READ-ONLY: ONE GET of a fixed path, by exec in the RUNNING dev data_store container against its own loopback port, the way its healthcheck probes it. NAME is one of ping (/ping), ui_config (/ui/v1/config), usage_summary (/ui/v1/stats/usage/summary); nothing else is accepted, no URL, path or header. GET only; Accept is the only header the script sets (http.client adds Host and Accept-Encoding: identity itself), nothing else is sent, no X-Instance-Secret; 10 s per socket operation, 20 s overall; at most the first 64 KB of the body is read and the cut is stated. Reports the HTTP status; the body and any error are redacted in the container. Refused when data_store is not running; use dev_db_tables then."""
        checked = stack.validate_dev_http_get_name(name)
        call.validated = {'name': checked}
        path = stack.DEV_HTTP_GET_PATHS[checked]
        try:
            target = await self._dev_exec_target(call, 'data_store')
        except stack.Refused as refusal:
            if str(refusal).startswith('no running data_store container'):
                raise stack.Refused(f'{refusal}; use dev_db_tables, which works while data_store is down') from None
            raise
        step = stack.dev_http_get_steps(self.settings.stack_dir, target, checked)[0]
        result = await call.step(step)
        status = stack.parse_http_get_status(result.stdout)
        call.details = {'name': checked, 'path': path, 'http_status': status}
        if result.exit_status != 0:
            if status is None:
                return 'failed', f'GET {path} got no HTTP response from the running dev data_store; see the steps'
            return 'failed', f'GET {path} returned HTTP {status} from the running dev data_store; see the steps'
        return 'ok', f'GET {path} returned HTTP {status} from the running dev data_store'

    # THE STATE-CHANGING LABEL-ONLY VERBS (tj-iee6sn.7). Each runs the same order, and a refusal at any
    # point runs nothing after it: the D7 prod guard (one `docker ps`), then -- dev_restart only, which
    # STARTS containers whose bind sources the daemon resolves at every start -- the D6 symlink check,
    # then the steering guard, then the budget charge (so only an admitted call is counted), then the
    # compose step. No snapshot, no git, no caller string in any argv.
    async def _mode_guard(
        self, call: _Call, accepted: frozenset[str | None] = stack.MODES_ANY, unaccepted: str = ''
    ) -> str | None:
        """D7 as the mode guard: the project's mode (DEV, TEST, None when empty); refuse any PROD container, mixed modes, or a mode ACCEPTED lacks."""
        result = await call.step(stack.dev_prod_guard_step(self.settings.stack_dir))
        return stack.check_mode_guard(result.exit_status, result.stdout, accepted, unaccepted)

    async def _dev_admit(self, *, binds: bool) -> None:
        """The checks between the prod guard and the compose step; BINDS adds the D6 symlink check."""
        if binds:
            await asyncio.to_thread(stack.check_dev_bind_sources, self.settings.repo_root)
        await asyncio.to_thread(stack.check_dev_steering_files, self.settings.stack_dir)
        self.dev_budget.charge()

    async def _dev_down(self, call: _Call) -> tuple[str, str]:
        """STATE-CHANGING: `docker compose -p trader_joe down` on the USER'S OWN dev compose project: its containers and project networks are stopped and removed. It never removes a volume or an image and never passes --remove-orphans; the database is a host bind and survives. It reads no compose file and no env file, takes no argument and builds nothing. Refused when the project holds a container started from neither the dev nor the test files (a prod launch) or dev and test containers together, when a compose file or dotenv file sits in the stack directory or a parent, and after 12 state-changing dev calls in an hour."""
        call.validated = {}
        await self._mode_guard(call)
        await self._dev_admit(binds=False)
        if not await call.steps_until_failure(stack.dev_down_steps(self.settings.stack_dir)):
            return 'failed', f'compose down failed for the {stack.DEV_PROJECT} project'
        return 'ok', f'the {stack.DEV_PROJECT} compose project is down; no volume or image was removed'

    async def _dev_restart(self, call: _Call, service: object) -> tuple[str, str]:
        """STATE-CHANGING: `docker compose -p trader_joe restart <service>` on the USER'S OWN dev compose project, SERVICE one of data_store, data_ingest, web (never postgres). Restarts the existing container with its existing image and configuration; it creates, removes and rebuilds nothing and loads no compose file or env file. Refused when the project holds a container started from neither the dev nor the test files (a prod launch) or dev and test containers together, when a repository bind source is a symlink, when a compose file or dotenv file sits in the stack directory or a parent, and after 12 state-changing dev calls in an hour."""
        checked = stack.validate_dev_restart_service(service)
        call.validated = {'service': checked}
        await self._mode_guard(call)
        await self._dev_admit(binds=True)
        if not await call.steps_until_failure(stack.dev_restart_steps(self.settings.stack_dir, checked)):
            return 'failed', f'compose restart of {checked} failed for the {stack.DEV_PROJECT} project'
        return 'ok', f'{checked} restarted in the {stack.DEV_PROJECT} project'

    # dev_build (tj-iee6sn.8) is a PLAIN call, not a job, and starts nothing, so none of the guards above
    # apply: no prod guard, no symlink check (no container, no bind), no steering walk (every -f is given
    # and the env is the placeholder one). Order, a refusal at any point running nothing after it: the S6
    # stale-copy check, the generated-code freshness, the budget (so only an admitted call is counted),
    # the two digests from the TRUSTED source_digest.sh (a failed or malformed digest stops the call
    # before the build: an unstamped image is the quiet failure tj-yb1bxj forbids), then the build.
    async def _dev_build(self, call: _Call) -> tuple[str, str]:
        """STATE-CHANGING: builds the :dev images of the USER'S OWN dev compose project (data_store, data_ingest, web) from the checkout, as make dev-build does and stamped with the same source digests. Runs to completion, up to 30 minutes (a cold build takes minutes): it returns ok or failed itself and is not a background job. It builds with the MCP image's baked copies of the compose files and Dockerfile and reads no env file; it creates and starts no container and removes nothing. Refused when the MCP image is stale (run make agent-mcp-rebuild on the host), when gen/proto/python or gen/proto/ts is missing or older than the .proto files (run make proto gen-proto-ts in the root checkout), and after 12 state-changing dev calls in an hour."""
        call.validated = {}
        await asyncio.to_thread(stack.check_trusted_copies_current, self.settings.repo_root)
        await asyncio.to_thread(stack.check_generated_code_fresh, self.settings.repo_root)
        self.dev_budget.charge()
        digests = []
        for step in stack.dev_digest_steps(self.settings.stack_dir, self.settings.repo_host_path):
            result = await call.step(step)
            digest = stack.parse_source_digest(result.exit_status, result.stdout)
            if digest is None:
                return 'failed', f'the source digest failed ({" ".join(step.argv[-2:])}); nothing was built'
            digests.append(digest)
        build = stack.dev_build_step(self.settings.stack_dir, self.settings.repo_host_path, digests)
        if not await call.steps_until_failure([build]):
            return 'failed', f'compose build failed for the {stack.DEV_PROJECT} project'
        return 'ok', f'the {stack.DEV_PROJECT} :dev images (data_store, data_ingest, web) are built and stamped'

    # dev_up (tj-iee6sn.9) is a PLAIN call. Order, a refusal at any point running nothing after it and
    # charging nothing: the mode guard (any non-PROD mode; a TEST stack is stopped by the down), the S6 stale-copy check, the D6 symlink check, the D5 env-file
    # preflight, the steering guard (the label-only down searches the stack directory), the generated-code
    # freshness, the three :dev images present, the budget, the two digests (a failed digest returns
    # BEFORE the down, so it never leaves the stack down), the base images (a failed pull likewise stops
    # before the down), the label-only down, the devnet network, and
    # the up, wrapped to supply the instance secret in the child (stack.DEV_UP_WRAPPER_SCRIPT).
    async def _dev_up(self, call: _Call) -> tuple[str, str]:
        """STATE-CHANGING: `make dev-up` on the USER'S OWN dev compose project. Stops the project first (docker compose -p trader_joe down: it never removes a volume or an image, and the database is a host bind), so there is never more than one instance; a foreground make dev-launch session is ended by that. Then makes sure the trader_joe_devnet network exists and starts postgres, data_store, data_ingest and web detached from the MCP image's baked compose files, waiting up to 300 s for healthy (a missing postgres container is created). Takes no argument and never builds: the three :dev images must exist, else it is refused and says to call dev_build. Compose reads the root env file itself; the instance write secret is derived in the child process from it (as make dev-up does) and never passes through this server. Refused when the project holds a container started from neither the dev nor the test files (a prod launch) or dev and test containers together, when the MCP image is stale (run make agent-mcp-rebuild), when a repository bind source or an env file is a symlink or not a regular file, when a compose file or dotenv file sits in the stack directory or a parent, when gen/proto/python or gen/proto/ts is missing or older than the .proto files, and after 12 state-changing dev calls in an hour. Up to 10 minutes; returns ok or failed itself and is not a background job."""
        call.validated = {}
        settings = self.settings
        await self._mode_guard(call)
        await asyncio.to_thread(stack.check_trusted_copies_current, settings.repo_root)
        await asyncio.to_thread(stack.check_dev_bind_sources, settings.repo_root)
        await asyncio.to_thread(stack.env_file_preflight, settings.repo_root)
        await asyncio.to_thread(stack.check_dev_steering_files, settings.stack_dir)
        await asyncio.to_thread(stack.check_generated_code_fresh, settings.repo_root)
        for step in stack.dev_image_steps(settings.stack_dir):
            if (await call.step(step)).exit_status != 0:
                raise stack.Refused(f'the image {step.argv[-1]} is not on the daemon: call dev_build first')
        self.dev_budget.charge()
        digests = []
        for step in stack.dev_digest_steps(settings.stack_dir, settings.repo_host_path):
            result = await call.step(step)
            digest = stack.parse_source_digest(result.exit_status, result.stdout)
            if digest is None:
                return (
                    'failed',
                    f'the source digest failed ({" ".join(step.argv[-2:])}); nothing was stopped or started',
                )
            digests.append(digest)
        # The base images are pulled HERE, before anything is stopped: the up step is flagged as building,
        # so the runner would otherwise ensure them just before the up, after the down, and a failed pull
        # would leave the project down. The up's own ensure is then a no-op (bases_present).
        await call.ensure_bases(settings.stack_dir)
        if not await call.steps_until_failure(stack.dev_down_steps(settings.stack_dir)):
            return 'failed', f'compose down failed for the {stack.DEV_PROJECT} project; nothing was started'
        inspect, create = stack.dev_network_steps(settings.stack_dir)
        # Look, else create, else look again (a create that lost a race to another creator still leaves it there).
        network_ready = (await call.step(inspect)).exit_status == 0
        if not network_ready:
            await call.step(create)
            network_ready = (await call.step(inspect)).exit_status == 0
        if not network_ready:
            return 'failed', f'the {stack.DEV_NETWORK} network could not be created; the project is down'
        up = stack.dev_up_step(settings.stack_dir, settings.repo_host_path, digests)
        if not await call.steps_until_failure([up]):
            return 'failed', f'compose up failed for the {stack.DEV_PROJECT} project; see the steps and dev_logs'
        return (
            'ok',
            f'the {stack.DEV_PROJECT} project was stopped and postgres, data_store, data_ingest and web are up and healthy',
        )

    # dev_tools (tj-iee6sn.14) is a PLAIN call, `make dev-tools`. Order, a refusal at any point running
    # nothing after it and charging nothing: the D7 prod guard, the S6 stale-copy check (the tools file
    # included), the D6 symlink check, the D5 env-file preflight, the budget, the devnet network, then the
    # wrapped up of pgAdmin. No steering walk (every -f is given, nothing label-only runs), no generated
    # code and no image checks (pgAdmin and postgres are image-only), no digest. The PGADMIN credentials
    # are read by compose from the root env file: this server never opens, passes or prints them.
    async def _dev_tools(self, call: _Call) -> tuple[str, str]:
        """STATE-CHANGING: `make dev-tools` on the USER'S OWN dev compose project: makes sure the trader_joe_devnet network exists and starts pgAdmin (with postgres, through its depends_on, if it is not up) detached from the MCP image's baked dev compose files plus docker-compose.tools.yaml, waiting for it to run. pgAdmin listens on 127.0.0.1 at POSTGRES_UI_PORT on the host. Takes no argument and never builds or stops anything. Compose reads PGADMIN_EMAIL and PGADMIN_PASS from the root env file itself: this server never reads, passes or prints them, and when either is unset compose refuses and the step says which variable to set, never a value. The instance write secret is derived in the child process as for dev_up. dev_down removes pgAdmin with the rest of the project. Refused when the project holds a container started from neither the dev nor the test files (a prod launch) or dev and test containers together, when the MCP image is stale (run make agent-mcp-rebuild), when a repository bind source or an env file is a symlink or not a regular file, and after 12 state-changing dev calls in an hour. Up to 5 minutes; returns ok or failed itself and is not a background job."""
        call.validated = {}
        settings = self.settings
        await self._mode_guard(call, stack.MODES_DEV_OR_EMPTY, 'test mode is up: dev_up first')
        await asyncio.to_thread(stack.check_trusted_copies_current, settings.repo_root)
        await asyncio.to_thread(stack.check_dev_bind_sources, settings.repo_root)
        await asyncio.to_thread(stack.env_file_preflight, settings.repo_root)
        self.dev_budget.charge()
        inspect, create = stack.dev_network_steps(settings.stack_dir)
        network_ready = (await call.step(inspect)).exit_status == 0
        if not network_ready:
            await call.step(create)
            network_ready = (await call.step(inspect)).exit_status == 0
        if not network_ready:
            return 'failed', f'the {stack.DEV_NETWORK} network could not be created; pgAdmin was not started'
        tools = stack.dev_tools_step(settings.stack_dir, settings.repo_host_path)
        if not await call.steps_until_failure([tools]):
            return 'failed', (
                f'compose up of pgadmin failed for the {stack.DEV_PROJECT} project; if the step output names '
                'PGADMIN_EMAIL or PGADMIN_PASS, set it in the root env file (the pgAdmin password needs 6+ '
                'characters); see the steps and dev_logs'
            )
        return 'ok', f'pgAdmin is running in the {stack.DEV_PROJECT} project; stop it with dev_down'

    # migrate and migrate_status (tj-iee6sn.10) are PLAIN calls on WHICHEVER mode of the one project is up
    # (DEV or TEST; ADR tj-4rr0la addenda 26-27). The mode guard runs first and alone: a PROD container,
    # both modes together, or an empty project is refused with no further command. migrate's order after
    # it, a refusal at any point running nothing after it and charging nothing: the S6 stale-copy check, the
    # D6 symlink check (the mode's own bind sources), the D5 env-file preflight, the empty-versions guard of
    # run_migrations.sh, exactly one running postgres and at most one data_store, the data_store image of the
    # mode present, in TEST mode the source digests, the stamp of the :test data_store image the one-off runs (always) and the running-stamp gate
    # (the stamp of each RUNNING data_store and data_ingest container: a prod image bakes the code in), then the budget, then the one-off.
    # No steering walk: every -f is given and nothing label-only runs.
    async def _migrate_mode(self, call: _Call) -> str:
        """The mode (DEV or TEST) the project is in; Refused when it is empty, holds a PROD container or both modes."""
        hint = 'nothing is up: call dev_up (your dev data) or test_up first'
        return await self._mode_guard(call, stack.MODES_DEV_OR_TEST, hint)

    async def _migrate_admit(self, call: _Call, mode: str) -> tuple[str, str] | None:
        """The preconditions of migrate after the mode guard; Refused at the first that fails, or a failed result when a digest could not be computed."""
        settings = self.settings
        test = mode == stack.MODE_TEST
        await asyncio.to_thread(stack.check_trusted_copies_current, settings.repo_root)
        bind_sources = stack.TEST_BIND_SOURCES if test else stack.DEV_BIND_SOURCES
        await asyncio.to_thread(stack.check_dev_bind_sources, settings.repo_root, bind_sources)
        await asyncio.to_thread(stack.env_file_preflight, settings.repo_root)
        versions = settings.repo_root / stack.MIGRATION_VERSIONS_DIR
        # run_migrations.sh's guard: upgrade head over no revisions is a successful no-op.
        if not await asyncio.to_thread(lambda: versions.is_dir() and any(versions.glob('*.py'))):
            raise stack.Refused(f'no revision files in {stack.MIGRATION_VERSIONS_DIR}: wrong checkout?')
        await self._dev_exec_target(call, 'postgres')
        lookup = await call.step(stack.dev_lookup_step(settings.stack_dir, 'data_store'))
        if lookup.exit_status != 0:
            raise stack.Refused(f'the container lookup failed (exit {lookup.exit_status}); see the steps')
        stack.refuse_several_dev_containers('data_store', lookup.stdout)
        if test:
            image_step = stack.testmode_data_store_image_step(settings.stack_dir)
            fix = 'test_build'
        else:
            image_step = stack.dev_data_store_image_step(settings.stack_dir)
            fix = 'dev_build'
        if (await call.step(image_step)).exit_status != 0:
            raise stack.Refused(f'the image {image_step.argv[-1]} is not on the daemon: call {fix} first')
        if not test:
            return None
        digests, failed = await self._source_digests(call)
        if failed is not None:
            return 'failed', f'the source digest failed ({failed}); nothing was migrated'
        # The :test image is what the one-off runs, so its stamp is read always: postgres alone up, or the
        # image rebuilt since test_up, would otherwise slip past the running-container gate below.
        stamp = await call.step(stack.dev_stamp_step(settings.stack_dir, stack.TEST_IMAGES[0]))
        stack.check_dev_image_stamp(stamp.exit_status, stamp.stdout, digests[0], stack.TEST_IMAGES[0])
        return await self._running_stamps_current(call, lookup.stdout, digests)

    async def _running_stamps_current(
        self, call: _Call, data_store_lookup: bytes, digests: list[str]
    ) -> tuple[str, str] | None:
        """TEST mode's gate: every RUNNING data_store and data_ingest container carries the checkout's source stamp, else Refused."""
        settings = self.settings
        ingest = await call.step(stack.dev_lookup_step(settings.stack_dir, 'data_ingest'))
        if ingest.exit_status != 0:
            raise stack.Refused(f'the container lookup failed (exit {ingest.exit_status}); see the steps')
        running = {
            service: stack.optional_dev_target(service, stdout)
            for service, stdout in (('data_store', data_store_lookup), ('data_ingest', ingest.stdout))
        }
        if not any(running.values()):
            return None
        for (service, target), digest in zip(running.items(), digests, strict=True):
            if target is None:
                continue
            stamp = await call.step(stack.container_stamp_step(settings.stack_dir, target))
            stack.check_container_stamp(stamp.exit_status, stamp.stdout, digest, service)
        return None

    async def _migrate(self, call: _Call) -> tuple[str, str]:
        """STATE-CHANGING, and it WRITES THE DATABASE OF THE MODE THAT IS UP: `make migrate` on the RUNNING stack of the one project, the USER'S OWN dev database in DEV mode and the ./volume_data/test database in TEST mode. It chooses the mode itself (the message starts with which, as make migrate prints) and runs `alembic upgrade head` in a one-off data_store container (docker compose run --rm --no-deps) from the MCP image's baked compose files, against the postgres that is running now, applying the revisions in the checkout's server/data/store/migrations/versions. DEV: the dev files and the :dev image. TEST: the base file and docker-compose.fake.yaml and the :test image, which bakes the code in, so it is also REFUSED unless the :test data_store image and each running data_store and data_ingest container carry a source stamp equal to this checkout's digest (stale test stack: call test_build, then test_up). Takes no argument. Never touches prod: refused when nothing is up (call dev_up or test_up), when the project holds a container started from neither the dev nor the test files (a prod launch) or dev and test containers together, when no or several postgres or several data_store containers run (in TEST also several data_ingest), when the checkout holds no revision files, when the data_store image of the mode does not exist (call dev_build or test_build), when the MCP image is stale (run make agent-mcp-rebuild), when a repository bind source or an env file is a symlink or not a regular file, and after 12 state-changing dev calls in an hour. Compose reads the env files itself; the instance write secret is derived in the child process and never passes through this server. There is no dry run and a migration can drop columns; the permission for this verb is the owner's client setting. Up to 10 minutes; returns ok or failed itself."""
        call.validated = {}
        settings = self.settings
        mode = await self._migrate_mode(call)
        selection = stack.migrate_selection(mode)
        failure = await self._migrate_admit(call, mode)
        if failure is not None:
            return failure
        self.dev_budget.charge()
        call.details = {'mode': mode, 'selection': selection}
        if mode == stack.MODE_TEST:
            step = stack.testmode_migrate_step(settings.stack_dir, settings.repo_host_path)
        else:
            step = stack.dev_migrate_step(settings.stack_dir, settings.repo_host_path)
        if not await call.steps_until_failure([step]):
            return (
                'failed',
                f'{selection}; alembic upgrade head failed in the one-off data_store container; see the steps',
            )
        return 'ok', f'{selection}; alembic upgrade head exited 0'

    async def _migrate_status(self, call: _Call) -> tuple[str, str]:
        """READ-ONLY: `alembic current` then `alembic history` on the mode that is up (DEV or TEST; the message starts with which), run by exec in the RUNNING data_store container with that container's own environment and its mounted migrations. Not make migrate-status's spelling (no one-off container, no env file, no compose file): it reports the revisions the running container sees. Refused when nothing is up (call dev_up or test_up), when the project holds a PROD container or both modes, and when data_store is not running (use dev_db_tables, which works while data_store is down). Takes no argument, charges no budget and writes nothing to the database (Python may write bytecode caches into the bind-mounted checkout, as make migrate-status does); every printed line, errors included, is redacted."""
        call.validated = {}
        mode = await self._migrate_mode(call)
        selection = stack.migrate_selection(mode)
        call.details = {'mode': mode, 'selection': selection}
        try:
            target = await self._dev_exec_target(call, 'data_store')
        except stack.Refused as refusal:
            if str(refusal).startswith('no running data_store container'):
                raise stack.Refused(f'{refusal}; use dev_db_tables, which works while data_store is down') from None
            raise
        step = stack.dev_migrate_status_steps(self.settings.stack_dir, target)[0]
        result = await call.step(step)
        if result.exit_status != 0:
            return (
                'failed',
                f'{selection}; alembic exited {result.exit_status} in the running data_store; see the steps',
            )
        return 'ok', f'{selection}; alembic current and history from the running data_store container'

    async def _source_digests(self, call: _Call) -> tuple[list[str], str | None]:
        """The two source digests (data_store, data_ingest) from the trusted source_digest.sh, or (digests so far, the failed one's words)."""
        settings = self.settings
        digests = []
        for step in stack.dev_digest_steps(settings.stack_dir, settings.repo_host_path):
            result = await call.step(step)
            digest = stack.parse_source_digest(result.exit_status, result.stdout)
            if digest is None:
                return digests, ' '.join(step.argv[-2:])
            digests.append(digest)
        return digests, None

    # test_build (tj-iee6sn.24) is `make test-build` as a PLAIN call, dev_build's order and reasoning: the S6
    # stale-copy check (the fake and test-overlay files included), the generated-code freshness (gen/proto/python
    # only: test mode has no web), the budget, the two digests, then the build, with the bases made present
    # first by the runner. No mode guard: it builds images and starts nothing. A refusal runs nothing after it.
    async def _test_build(self, call: _Call) -> tuple[str, str]:
        """STATE-CHANGING: builds the TEST-mode images (data_store and data_ingest, prod target, tagged by docker-compose.fake.yaml) from the root checkout, as make test-build does and stamped with the same source digests, through the MCP image's baked base file, docker-compose.fake.yaml and test overlay (which sets only build.dockerfile). It never builds or tags :latest, which may be a real deployment's image: no file set it can use names that tag. Runs to completion, up to 30 minutes (a cold build takes minutes): it returns ok or failed itself and is not a background job. It reads no env file and creates and starts no container. Refused when the MCP image is stale (run make agent-mcp-rebuild on the host), when gen/proto/python is missing or older than the .proto files (run make proto in the root checkout), and after 12 state-changing dev calls in an hour."""
        call.validated = {}
        settings = self.settings
        await asyncio.to_thread(stack.check_trusted_copies_current, settings.repo_root)
        await asyncio.to_thread(
            stack.check_generated_code_fresh, settings.repo_root, stack.TEST_GENERATED_DIRS, stack.TEST_GENERATE_HINT
        )
        self.dev_budget.charge()
        digests, failed = await self._source_digests(call)
        if failed is not None:
            return 'failed', f'the source digest failed ({failed}); nothing was built'
        build = stack.testmode_build_step(settings.stack_dir, settings.repo_host_path, digests)
        if not await call.steps_until_failure([build]):
            return 'failed', f'compose build of the test images failed for the {stack.DEV_PROJECT} project'
        return 'ok', 'the :test images (data_store, data_ingest) are built and stamped; :latest was not touched'

    # test_up (tj-iee6sn.24) is `make system-launch` as a PLAIN call. Order, a refusal at any point running
    # nothing after it and charging nothing: the mode guard (any non-PROD state), the S6 stale-copy check, the
    # D6 symlink check (stack.TEST_BIND_SOURCES), the D5 env-file preflight, the steering guard (the label-only down
    # searches the stack directory), the two :test images present, the two source digests, the two stamp reads
    # (each must equal its digest), then the budget, the bases (a failed pull stops before the down), the
    # label-only down and the up. No wrapper and no network step: the base and fake files need neither.
    async def _test_up(self, call: _Call) -> tuple[str, str]:
        """STATE-CHANGING: `make system-launch` on the USER'S OWN compose project, in TEST mode. Stops the project first (docker compose -p trader_joe down: it never removes a volume or an image), dev mode included, so only one mode is ever up; then starts postgres, data_store and data_ingest from the MCP image's baked base file and docker-compose.fake.yaml, waiting up to 300 s for healthy: the :test prod images, data_ingest on the fake broker (no vendor, no key; its log starts 'FAKE BROKER:'), postgres on ./volume_data/test, never your dev data and never :latest. Takes no argument and never builds: both :test images must exist and carry a source stamp equal to this checkout's digest, else it is refused and says to call test_build (STALE: call test_build when the checkout moved on). up or dev_up returns to dev mode. Compose reads the root env file itself. Refused when the project holds a container started from neither the dev nor the test files (a prod launch) or dev and test containers together, when the MCP image is stale (run make agent-mcp-rebuild), when a repository bind source (data_store alembic.ini and migrations, tests/fakes) or an env file is a symlink or not a regular file, when a compose file or dotenv file sits in the stack directory or a parent, and after 12 state-changing dev calls in an hour. Up to 10 minutes; returns ok or failed itself and is not a background job."""
        call.validated = {}
        settings = self.settings
        await self._mode_guard(call)
        await asyncio.to_thread(stack.check_trusted_copies_current, settings.repo_root)
        await asyncio.to_thread(stack.check_dev_bind_sources, settings.repo_root, stack.TEST_BIND_SOURCES)
        await asyncio.to_thread(stack.env_file_preflight, settings.repo_root)
        await asyncio.to_thread(stack.check_dev_steering_files, settings.stack_dir)
        for step in stack.testmode_image_steps(settings.stack_dir):
            if (await call.step(step)).exit_status != 0:
                raise stack.Refused(f'the image {step.argv[-1]} is not on the daemon: call test_build first')
        digests, failed = await self._source_digests(call)
        if failed is not None:
            return 'failed', f'the source digest failed ({failed}); nothing was stopped or started'
        for image, digest in zip(stack.TEST_IMAGES, digests, strict=True):
            stamp = await call.step(stack.dev_stamp_step(settings.stack_dir, image))
            stack.check_dev_image_stamp(stamp.exit_status, stamp.stdout, digest, image)
        self.dev_budget.charge()
        await call.ensure_bases(settings.stack_dir)
        if not await call.steps_until_failure(stack.dev_down_steps(settings.stack_dir)):
            return 'failed', f'compose down failed for the {stack.DEV_PROJECT} project; nothing was started'
        up = stack.testmode_up_step(settings.stack_dir, settings.repo_host_path)
        if not await call.steps_until_failure([up]):
            return (
                'failed',
                f'compose up failed for the {stack.DEV_PROJECT} project in test mode; see the steps and dev_logs',
            )
        return (
            'ok',
            f'the {stack.DEV_PROJECT} project was stopped and test mode is up and healthy: postgres on '
            f'{stack.TEST_DATA_DIR}, data_store and data_ingest from the :test images, data_ingest on the fake broker',
        )

    @property
    def verbs(self) -> tuple[str, ...]:
        """The verb names, in the ADR's order."""
        return tuple(self._handlers)

    def describe(self, verb: str) -> str:
        """A verb's description for the MCP tool list: its handler's docstring, on one line."""
        return ' '.join((self._handlers[verb].__doc__ or verb).split())
