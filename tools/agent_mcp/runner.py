"""The verbs: one at a time, each under a timeout, with captured and truncated output and one audit line.

No MCP import here, so the dev venv (which does not install the agent-mcp group) can import and test
all of it; server.py only exposes AgentStack.call over MCP.

OFF THE EVENT LOOP: every blocking step -- the git plumbing (worktree list, the committed defaults),
the snapshot copy, verify and swap, and the data-directory removal -- runs through asyncio.to_thread.
A blocked loop could neither answer 'busy' nor fire a verb's timeout. The lock is taken before the
first await, so it still admits one verb at a time.
"""

import asyncio
import errno
import json
import os
import shlex
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
    'migrate': 600,
    'migrate_status': 300,
    'run_system_tests': 1800,
    # run_system_tests' budget, for the same work: a snapshot, a test_client build that on a cold
    # cache re-syncs the image's dependencies, then the producer's scenario POSTs, the fake ingest
    # behind them and the dump. 30 s was the not_available answer's.
    'seed_dump': 1800,
    'logs': 60,
    'ps': 60,
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
}
for _schema in VERB_SCHEMAS.values():
    _schema.update(type='object', additionalProperties=False)

_OPTIONAL_DEFAULTS: dict[str, dict[str, Any]] = {'logs': {'tail': stack.TAIL_DEFAULT}}

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


async def _read_capped(stream: asyncio.StreamReader, cap: int) -> bytes:
    """Read a stream to its end, keeping at most cap + 1 bytes: one over says 'over the cap' without the rest."""
    kept = bytearray()
    while chunk := await stream.read(_READ_CHUNK):
        if len(kept) <= cap:
            kept += chunk[: cap + 1 - len(kept)]
    return bytes(kept)


async def run_process(step: stack.Step) -> ProcessResult:
    """Run one argument list -- never a shell -- in DOCKER_ENV, capturing both streams.

    A step with stdout_cap has its stdout read to the end but kept only up to the cap plus one byte,
    so a runaway producer cannot fill the server's memory. Cancelled (the verb's timeout), it kills
    the process before letting the cancellation through, so no docker client outlives the verb that
    started it.
    """
    process = await asyncio.create_subprocess_exec(
        *step.argv,
        cwd=step.cwd,
        env=DOCKER_ENV,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        if step.stdout_cap is None or process.stdout is None or process.stderr is None:
            stdout, stderr = await process.communicate()
        else:
            stdout, stderr = await asyncio.gather(_read_capped(process.stdout, step.stdout_cap), process.stderr.read())
            await process.wait()
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
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
    """The nine verbs over the one agent stack, serialised: a second caller is told 'busy'."""

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
        }

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

    async def _recorded_worktree(self) -> Path:
        """The worktree the last stack_up named, re-resolved; refused when none is recorded or it is gone."""
        worktrees = await self._worktrees()
        state = self._state_file()
        if not state.is_symlink() and state.is_file():
            name = state.read_text().strip()
            if name in worktrees:
                return worktrees[name]
        raise stack.Refused('no stack_up is recorded, or its worktree is gone: call stack_up first')

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

    async def _migrate(self, call: _Call) -> tuple[str, str]:
        """Apply the migrations of a fresh snapshot of the worktree the stack was brought up from (alembic upgrade head)."""
        return await self._alembic(call, ['upgrade', 'head'])

    async def _migrate_status(self, call: _Call) -> tuple[str, str]:
        """Read-only: alembic current, then alembic history, against the agent stack."""
        return await self._alembic(call, ['current'], ['history'])

    async def _alembic(self, call: _Call, *commands: list[str]) -> tuple[str, str]:
        path = await self._recorded_worktree()
        call.validated = {}
        snapshot = await self._refresh_snapshot(path)
        stack.check_mount_sources(snapshot)
        versions = snapshot / stack.MIGRATION_VERSIONS_DIR
        # The empty-versions guard of run_migrations.sh: upgrade head over no revisions is a
        # successful no-op, indistinguishable from a migration that worked.
        if not versions.is_dir() or not any(versions.glob('*.py')):
            raise stack.Refused(f'no revision files in {stack.MIGRATION_VERSIONS_DIR} of the snapshot')
        env_file = await self._guarded_env()
        stack_dir = self.settings.stack_dir
        running = await call.step(stack.postgres_running_steps(stack_dir, env_file)[0])
        if running.exit_status != 0 or not running.stdout.strip():
            return 'failed', 'postgres is not running in the agent stack: call stack_up first'
        if not await call.steps_until_failure(stack.alembic_steps(stack_dir, env_file, *commands)):
            return 'failed', 'alembic failed'
        return 'ok', 'alembic ' + '; '.join(' '.join(command) for command in commands)

    async def _run_system_tests(self, call: _Call, worktree: object, paths: object) -> tuple[str, str]:
        """Snapshot WORKTREE and run tests/system (or PATHS under it) from test_client, rebuilt from the snapshot, against the agent stack.

        Only test_client is rebuilt and recreated: the running services keep the code of the last
        stack_up, so after editing anything they load -- tests/fakes included -- call stack_up first.
        The disposable-database attestation of make test-system is satisfied by construction: this
        verb reaches the agent stack and nothing else.
        """
        name = stack.check_worktree_name(worktree)
        stack.check_test_paths(paths)
        path = stack.resolve_worktree(name, await self._worktrees())
        snapshot = await self._refresh_snapshot(path)
        stack.check_mount_sources(snapshot)
        resolved = stack.resolve_test_paths(snapshot, paths)
        call.validated = {'worktree': name, 'paths': resolved}
        env_file = await self._guarded_env()
        if not await call.steps_until_failure(stack.system_tests_steps(self.settings.stack_dir, env_file, resolved)):
            return 'failed', f'the system suite failed (pytest exit status {call.last_exit})'
        return 'ok', 'the system suite passed'

    async def _seed_dump(self, call: _Call, worktree: object, date: object = None) -> tuple[str, str]:
        """Snapshot WORKTREE, rebuild test_client from it and run the seed producer there against the agent stack (up, migrated, fake mode); write <revision>.sql and <revision>.json under agent_mcp_seeds/<worktree>/ in the share directory.

        DATE (YYYY-MM-DD, a real calendar date) is the manifest's; default today. The response names
        the two files RELATIVE to the share directory -- /agent_mcp_share, the same path in the
        devcontainer -- with their sizes and the manifest's row counts, never their content. Exit 3
        from the producer is 'refused'; a bundle the contract refuses, or stdout over its cap, is
        'failed' with nothing written. The producer runs per call in test_client, which has no Docker
        access and no writable mount; this process reads its stdout and writes the files itself, with
        no-follow writes (ADR tj-4rr0la addendum 10, relocated by addendum 11 R2).
        """
        name = stack.check_worktree_name(worktree)
        checked_date = stack.check_seed_date(date)
        path = stack.resolve_worktree(name, await self._worktrees())
        call.validated = {'worktree': name} if checked_date is None else {'worktree': name, 'date': checked_date}
        snapshot = await self._refresh_snapshot(path)
        stack.check_mount_sources(snapshot)
        env_file = await self._guarded_env()
        build, produce = stack.seed_dump_steps(self.settings.stack_dir, env_file, checked_date)
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

    @property
    def verbs(self) -> tuple[str, ...]:
        """The verb names, in the ADR's order."""
        return tuple(self._handlers)

    def describe(self, verb: str) -> str:
        """A verb's description for the MCP tool list: its handler's docstring, on one line."""
        return ' '.join((self._handlers[verb].__doc__ or verb).split())
