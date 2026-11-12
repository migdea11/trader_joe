"""A stub `docker` for the store's shell scripts: run_migrations.sh, restore_dump.sh and rehearse_migration.sh.

TIER. The real scripts, run by bash as subprocesses, with this stub first on PATH (the house pattern:
common/tests/test_roots.py, test_service_pythonpath.py). The stub answers by argv from files in a per-test
directory and journals every call, so each assertion reads the argv docker was actually given. No Docker runs.

WHAT IT ANSWERS, by argv (every file named lives in the per-test directory; FILE.rc, when present, is the exit
status, else 0):

* compose [-p trader_joe_dev] -f ... ps -q SERVICE: ps.PROJECT.SERVICE (PROJECT is dev or default).
* compose ... config --images data_store: :dev with the override, :test with the fake, else :latest.
* compose ... run ... --entrypoint ... with a word naming /code/migrations/versions: the empty-versions guard's look
  into the image (tj-7zjov1.45): revisions_probe. Absent by default, so it exits 0 (revisions present);
  revisions_probe.rc 3 is the image's own "no revision files", any other status a failure to look.
* compose ... run ... --entrypoint ...: the DEPLOYMENT_LABEL read run_migrations.sh makes on the production
  definition: label_read (empty by default, so the label is empty and the migrate proceeds).
* compose ... run ...: any other one-off, which is alembic: run.
* compose ... exec -T postgres sh -c '...pg_dump...': the dump's bytes: dump (non-empty by default).
* compose ... exec -T postgres pg_restore --list: reads its stdin into stdin.list.N (N counts from 0), then list.
* compose ... exec -T postgres sh -c '...psql...': restore_dump.sh's schema drop: psql.
* compose ... exec -T postgres sh -c '...pg_restore...': the load; reads its stdin into stdin.restore, then restore.
* inspect --format '...State.Health...' ID: health.ID. inspect.ID.rc fails the call.
* inspect --format '...config_files...' ID: label.ID. inspect.ID.rc fails the call.
* inspect --format '...Mounts...' ID: run_migrations.sh's look for the postgres data mount (tj-7zjov1.46):
  mounts.ID, which Harness.mounts renders from a list of Mount the way docker renders MOUNTS_FORMAT.
  mounts.ID.rc (or inspect.ID.rc) fails the call; with no mounts.ID at all it exits 93, so a look nobody
  configured fails loudly. A separate file from label.ID, so a failed mount look leaves the label read alone.
* image inspect: by image_mode (missing, unstamped, stale, match).

Anything else exits 93 to 98, so an unexpected call fails loudly rather than answering 0.
"""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from common.tests.roots import SERVER_ROOT


STORE_DIR = SERVER_ROOT / 'data' / 'store'
RUN_MIGRATIONS = STORE_DIR / 'run_migrations.sh'
RESTORE_DUMP = STORE_DIR / 'restore_dump.sh'
REHEARSE_MIGRATION = STORE_DIR / 'rehearse_migration.sh'

BASH = shutil.which('bash')

ALEMBIC = '/code/.venv/bin/alembic'
# What the stub's `compose run` prints, standing in for alembic's own stdout.
ALEMBIC_STDOUT = 'deadbeef0001 (head)'
# What the stub's pg_dump writes by default: non-empty, so the dump passes the size check.
DUMP_BYTES = 'PGDMP stand-in custom-format dump\n'

# Where the data_store image keeps the revisions: run_migrations.sh's IMAGE_MIGRATION_DIR. The builder's reading of
# the Dockerfile P04 (tj-7zjov1.10) lands; the architect confirms it against that Dockerfile.
IMAGE_MIGRATION_DIR = '/code/migrations/versions'

# The words after the -p/-f options of the empty-versions guard's look into the image (tj-7zjov1.45): the same one-off
# shape as the label read, exit 3 when the image holds no revision file.
REVISIONS_PROBE = ('run', '--rm', '--no-deps', '-T', '--entrypoint', '/bin/sh', 'data_store', '-c',
                   f'set -- {IMAGE_MIGRATION_DIR}/*.py; [ -e "$1" ] || exit 3')  # fmt: skip

# The words after the -p/-f options, per call the migration-safety gate adds (P03).
LABEL_READ =('run', '--rm', '--no-deps', '-T', '--entrypoint', '/bin/sh', 'data_store', '-c',
              'printf %s "${DEPLOYMENT_LABEL-}"')  # fmt: skip
PG_DUMP = ('exec', '-T', 'postgres', 'sh', '-c',
           'exec pg_dump --format=custom --user "$POSTGRES_USER" --dbname "$POSTGRES_DB"')  # fmt: skip
PG_RESTORE_LIST = ('exec', '-T', 'postgres', 'pg_restore', '--list')
PSQL_DROP = ('exec', '-T', 'postgres', 'sh', '-c',
             'exec psql --user "$POSTGRES_USER" --dbname "$POSTGRES_DB" -v ON_ERROR_STOP=1 '
             '-c "DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;"')  # fmt: skip
PG_RESTORE = ('exec', '-T', 'postgres', 'sh', '-c',
              'exec pg_restore --user "$POSTGRES_USER" --dbname "$POSTGRES_DB" --no-owner --exit-on-error '
              '--single-transaction')  # fmt: skip
ALEMBIC_RUN = ('run', '--rm', '--no-deps', 'data_store')

# Where the postgres image keeps its data, and the template run_migrations.sh hands `docker inspect` to find the host
# directory bind-mounted there (tj-7zjov1.46). Pinned word for word: Harness.mounts renders the template's meaning
# (each bind mount at PG_DATA, its source then a newline), so a template that changed would no longer be the one the
# double answers for, and the test pinning the argv says so.
PG_DATA = '/var/lib/postgresql/data'
MOUNTS_FORMAT = (
    '{{ range .Mounts }}{{ if eq .Destination "/var/lib/postgresql/data" }}{{ if eq .Type "bind" }}'
    '{{ println .Source }}{{ end }}{{ end }}{{ end }}'
)

# The one stderr line a successful dump prints; group 1 is the dump's path.
DUMP_LINE = re.compile(
    r'run_migrations\.sh: dump taken before the migration: (\S+) \(same disk as the database; copy it off the host\)'
)


STUB = r"""#! /bin/bash
# Stub docker for the store's shell-script tests (compose_double.py). Answers from files in $STUB_DIR; journals argv.
set -u
{
    for word in "$@"; do printf '%s\x1f' "$word"; done
    printf '\n'
} >> "$STUB_DIR/calls.log"

answer() {  # print $STUB_DIR/$1 if present; exit with $STUB_DIR/$1.rc if present, else 0
    if [ -f "$STUB_DIR/$1" ]; then cat "$STUB_DIR/$1"; fi
    if [ -f "$STUB_DIR/$1.rc" ]; then exit "$(cat "$STUB_DIR/$1.rc")"; fi
    exit 0
}

if [ "$1" = compose ]; then
    shift
    # -p and -f, in any order, before the subcommand. The only project the script may name is trader_joe_dev;
    # with none, the call is the default project's. Each project's ps answers come from their own files.
    files=()
    project=default
    # compose's own rule: COMPOSE_PROJECT_NAME names the project when no -p does; an explicit -p wins below.
    if [ "${COMPOSE_PROJECT_NAME:-}" = trader_joe_dev ]; then project=dev; fi
    while [ "${1:-}" = -f ] || [ "${1:-}" = -p ]; do
        if [ "$1" = -f ]; then
            files+=("$2")
        elif [ "$2" = trader_joe_dev ]; then
            project=dev
        else
            exit 96
        fi
        shift 2
    done
    case "$1" in
        ps) answer "ps.$project.$3" ;;
        config)
            # The image each list resolves to by design: the override sets :dev, the test-mode overlay :test
            # (addendum 27 (2), landing in tj-iee6sn.19), the base file alone :latest.
            for file in "${files[@]}"; do
                if [ "$file" = docker-compose.override.yaml ]; then echo trader_joe_data_store:dev; exit 0; fi
                if [ "$file" = docker-compose.fake.yaml ]; then echo trader_joe_data_store:test; exit 0; fi
            done
            echo trader_joe_data_store:latest
            exit 0
            ;;
        run)
            # The revisions probe first: it shares --entrypoint with the label read and is told apart by the
            # image path its command names.
            for word in "$@"; do
                case "$word" in *"/code/migrations/versions"*) answer revisions_probe ;; esac
            done
            for word in "$@"; do
                if [ "$word" = --entrypoint ]; then answer label_read; fi
            done
            answer run
            ;;
        exec)
            if [ "${2:-}" != -T ] || [ "${3:-}" != postgres ]; then exit 95; fi
            shift 3
            if [ "$1" = pg_restore ] && [ "${2:-}" = --list ]; then
                n=0
                for f in "$STUB_DIR"/stdin.list.*; do [ -e "$f" ] && n=$((n + 1)); done
                cat > "$STUB_DIR/stdin.list.$n"
                answer list
            fi
            if [ "$1" = sh ] && [ "${2:-}" = -c ]; then
                case "$3" in
                    *pg_dump*) answer dump ;;
                    *psql*) answer psql ;;
                    *pg_restore*) cat > "$STUB_DIR/stdin.restore"; answer restore ;;
                esac
            fi
            exit 94
            ;;
    esac
    exit 97
fi

if [ "$1" = inspect ]; then
    id="${!#}"
    if [ -f "$STUB_DIR/inspect.$id.rc" ]; then exit "$(cat "$STUB_DIR/inspect.$id.rc")"; fi
    case "${3:-}" in
        *State.Health*) answer "health.$id" ;;
        *.Mounts*)
            if [ ! -f "$STUB_DIR/mounts.$id" ] && [ ! -f "$STUB_DIR/mounts.$id.rc" ]; then exit 93; fi
            answer "mounts.$id"
            ;;
    esac
    if [ -f "$STUB_DIR/label.$id" ]; then cat "$STUB_DIR/label.$id"; fi
    printf '\n'
    exit 0
fi

if [ "$1" = image ] && [ "$2" = inspect ]; then
    mode="$(cat "$STUB_DIR/image_mode")"
    if [ "$mode" = missing ]; then exit 1; fi
    if [ "$3" != --format ]; then exit 0; fi
    case "$mode" in
        unstamped) printf '\n' ;;
        stale) printf '%064d\n' 0 ;;
        match) cat "$STUB_DIR/digest" ;;
    esac
    exit 0
fi
exit 98
"""


PROJECTS = ('default', 'dev')


@dataclass(frozen=True)
class Stack:
    """What the stub reports. labels maps a container id to its label; None means `docker inspect` fails."""

    postgres: tuple[str, ...]
    labels: dict[str, str | None] = field(default_factory=dict)
    data_store: tuple[str, ...] = ()
    data_store_ps_fails: bool = False


@dataclass(frozen=True)
class Mount:
    """One entry of a container's .Mounts, as docker inspect reports it: Type is bind or volume, Source a host path."""

    source: str
    destination: str = PG_DATA
    type: str = 'bind'


def render_mounts(mounts: list[Mount]) -> str:
    """What docker prints for MOUNTS_FORMAT: the source of each bind mount at PG_DATA, each followed by a newline."""
    return ''.join(f'{m.source}\n' for m in mounts if m.destination == PG_DATA and m.type == 'bind')


@dataclass(frozen=True)
class Call:
    argv: tuple[str, ...]

    @property
    def is_compose(self) -> bool:
        return self.argv[:1] == ('compose',)

    def _options(self) -> list[tuple[str, str]]:
        """The leading (-f|-p, value) pairs after `compose`, in the order given."""
        options, words = [], list(self.argv[1:])
        while words[:1] in (['-f'], ['-p']):
            options.append((words[0], words[1]))
            words = words[2:]
        return options

    @property
    def files(self) -> tuple[str, ...]:
        return tuple(value for flag, value in self._options() if flag == '-f')

    @property
    def project_args(self) -> tuple[str, ...]:
        """Every -p word pair, in order: () for the default project, ('-p', 'trader_joe_dev') for the dev one."""
        return tuple(word for flag, value in self._options() if flag == '-p' for word in (flag, value))

    @property
    def rest(self) -> tuple[str, ...]:
        return self.argv[1 + 2 * len(self._options()) :]


@dataclass
class Outcome:
    result: subprocess.CompletedProcess
    calls: list[Call]

    def compose(self, *subcommand: str) -> list[Call]:
        return [c for c in self.calls if c.is_compose and c.rest[: len(subcommand)] == subcommand]

    def only(self, *subcommand: str) -> Call:
        found = self.compose(*subcommand)
        assert len(found) == 1, f'expected one compose {subcommand} call, got {found}\n{self.explain()}'
        return found[0]

    def alembic(self) -> Call:
        """The one alembic one-off (`run --rm --no-deps data_store ...`), never the label read (`run ... -T`)."""
        return self.only(*ALEMBIC_RUN)

    def gate_calls(self) -> list[tuple[str, ...]]:
        """The rest of every revisions probe, label read, dump and read-back, in order."""
        gate = (REVISIONS_PROBE, LABEL_READ, PG_DUMP, PG_RESTORE_LIST)
        return [c.rest for c in self.calls if c.is_compose and c.rest in gate]

    def explain(self) -> str:
        return (
            f'exit {self.result.returncode}\nstdout:\n{self.result.stdout}\nstderr:\n{self.result.stderr}\n'
            f'docker calls:\n' + '\n'.join(' '.join(c.argv) for c in self.calls)
        )


class Harness:
    def __init__(self, tmp_path: Path, digest: str | None):
        self.dir = tmp_path / 'stub'
        (self.dir / 'bin').mkdir(parents=True)
        docker = self.dir / 'bin' / 'docker'
        docker.write_text(STUB, encoding='utf-8')
        docker.chmod(0o755)
        self.digest = digest
        # Where run_migrations.sh puts its dump when MIGRATE_BACKUP_DIR is set (the default here): never the checkout's
        # volume_data.
        self.backups = tmp_path / 'backups'
        self.image_mode('match')
        self.write('dump', DUMP_BYTES)
        self.write('label_read', '')

    def write(self, name: str, text: str) -> None:
        (self.dir / name).write_text(text, encoding='utf-8')

    def read(self, name: str) -> str:
        return (self.dir / name).read_text(encoding='utf-8')

    def revisions(self, status: int) -> None:
        """The exit status of the look into the image: 0 revisions present, 3 none, anything else a failure to look."""
        self.write('revisions_probe.rc', str(status))

    def image_mode(self, mode: str) -> None:
        self.write('image_mode', mode)

    def stack(self, stack: Stack, project: str = 'default', other: Stack | None = None) -> None:
        """Put STACK in PROJECT ('dev' is trader_joe_dev, 'default' is prod's); OTHER, if given, in the other one.

        With no OTHER the other project holds nothing: its ps answers are empty.
        """
        assert project in PROJECTS, project
        other_project = 'default' if project == 'dev' else 'dev'
        self._place(stack, project)
        self._place(other or Stack(()), other_project)

    def _place(self, stack: Stack, project: str) -> None:
        self.write(f'ps.{project}.postgres', ''.join(f'{i}\n' for i in stack.postgres))
        self.write(f'ps.{project}.data_store', ''.join(f'{i}\n' for i in stack.data_store))
        if stack.data_store_ps_fails:
            self.write(f'ps.{project}.data_store.rc', '1')
        for container, label in stack.labels.items():
            if label is None:
                self.write(f'inspect.{container}.rc', '1')
            else:
                self.write(f'label.{container}', label)

    def mounts(self, container: str, mounts: list[Mount] | None) -> None:
        """What `docker inspect` of CONTAINER's mounts answers: the rendered MOUNTS; None makes the inspect fail."""
        if mounts is None:
            self.write(f'mounts.{container}.rc', '1')
        else:
            self.write(f'mounts.{container}', render_mounts(mounts))

    def backup_files(self) -> list[Path]:
        return sorted(self.backups.iterdir()) if self.backups.exists() else []

    def run(
        self,
        *args: str,
        run_status: int = 0,
        extra_env: dict[str, str] | None = None,
        script: Path = RUN_MIGRATIONS,
        alembic_stdout: str = ALEMBIC_STDOUT,
        umask: int = -1,
        backup_dir_set: bool = True,
    ) -> Outcome:
        """Run SCRIPT with ARGS under the stub docker.

        MIGRATE_BACKUP_DIR is self.backups unless BACKUP_DIR_SET is False, which leaves it unset (as is DATA_DIR, unless
        EXTRA_ENV sets it), so run_migrations.sh takes the dump folder from the postgres data mount.
        """
        self.write('run', alembic_stdout + '\n')
        self.write('run.rc', str(run_status))
        if self.digest is not None:
            self.write('digest', self.digest + '\n')
        log = self.dir / 'calls.log'
        log.write_text('', encoding='utf-8')
        stripped = (
            'MIGRATE_CHECK_ALLOW_STALE_IMAGE', 'COMPOSE_PROJECT_NAME', 'MIGRATE_BACKUP_DIR', 'DATA_DIR',
            'SYSTEM_TEST_DISPOSABLE_DB',
        )  # fmt: skip
        env = {k: v for k, v in os.environ.items() if k not in stripped}
        env |= {'PATH': f'{self.dir / "bin"}{os.pathsep}{os.environ["PATH"]}', 'STUB_DIR': str(self.dir)}
        if backup_dir_set:
            env['MIGRATE_BACKUP_DIR'] = str(self.backups)
        env |= extra_env or {}
        result = subprocess.run(
            [BASH, str(script), *args],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=120,
            umask=umask,
            stdin=subprocess.DEVNULL,
        )
        calls = [Call(tuple(line.split('\x1f')[:-1])) for line in log.read_text(encoding='utf-8').splitlines()]
        return Outcome(result, calls)
