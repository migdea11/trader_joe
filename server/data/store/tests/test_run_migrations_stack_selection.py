"""run_migrations.sh runs alembic through the definition of the stack that is RUNNING (tj-s6r3rt.1).

THE DESIGN is tj-s6r3rt.1's DETECTION DESIGN, from decision tj-slxynv option (b), the tj-x3ig38 addendum of
2026-10-12 and tj-yb1bxj addendum 4:

* the postgres container's compose label com.docker.compose.project.config_files is read; the stack is DEV iff
  some entry's FILE NAME is exactly docker-compose.override.yaml -- never a full-path comparison, since the label
  holds the paths as whoever ran `up` saw them (a host path, or a devcontainer's /workspace);
* a dev postgres is cross-checked against a running data_store, whose label must say dev too;
* DEV runs `-f docker-compose.yaml -f docker-compose.override.yaml` and nothing else; EVERY other case --
  prod, system-launch, no label, empty, malformed, relative, a failed inspect or ps, more than one candidate
  container, a mixed stack -- runs `-f docker-compose.yaml` alone, byte-identical to the command before the
  change, and STILL RUNS ALEMBIC: a selection problem never refuses `upgrade head`;
* exactly ONE line on stderr names the definition and why; stdout gains nothing (callers parse `alembic current`);
* the exit status is alembic's, not the detection's;
* `check`'s comparison-image guard asks the SELECTED definition which image runs, and its FIX: line names the
  build for that definition: make dev-build on DEV, make prod-build on PROD.

THE LABEL FORMAT IS ASSUMED, NOT OBSERVED. Absolute paths joined by commas is compose v2's documented behaviour as
the design states it; the owner's confirmation on a real host is tj-s6r3rt.6, open when these were written. If
the host shows a different format, the classifier table below is what changes.

TIER. The real script, run by bash as a subprocess, with a stub `docker` first on PATH (the house pattern:
common/tests/test_roots.py, test_service_pythonpath.py). The stub answers by argv from files in a per-test
directory and journals every call, so each assertion reads the argv compose was actually given. No Docker runs.
The classifier itself is also driven directly, from the block run_migrations.sh marks for extraction.
"""

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from common.tests.roots import REPO_ROOT, SERVER_ROOT


pytestmark = pytest.mark.data_store

SCRIPT = SERVER_ROOT / 'data' / 'store' / 'run_migrations.sh'
MIGRATION_DIR = SERVER_ROOT / 'data' / 'store' / 'migrations' / 'versions'

BASE = 'docker-compose.yaml'
OVERRIDE = 'docker-compose.override.yaml'
PROD_FILES = (BASE,)
DEV_FILES = (BASE, OVERRIDE)
ALEMBIC = '/code/.venv/bin/alembic'
# What the stub's `compose run` prints, standing in for alembic's own stdout.
ALEMBIC_STDOUT = 'deadbeef0001 (head)'

DEV_LINE = (
    'run_migrations.sh: using the DEV definition (docker-compose.yaml + docker-compose.override.yaml): '
    'the running postgres was started with docker-compose.override.yaml'
)
PROD_PREFIX = 'run_migrations.sh: using the PRODUCTION definition (docker-compose.yaml): '

# Two roots, because the design's whole reason for a basename match is that they differ.
HOST = '/home/owner/src/trader_joe'
DEVCONTAINER = '/workspace'


def _label(root: str, *names: str) -> str:
    return ','.join(f'{root}/{name}' for name in names)


# DEV_COMPOSE (Makefile) loads the base, the override and the two web files; a bare `docker compose up` loads
# the base and the override; PROD_COMPOSE the base and the web file; SYSTEM_COMPOSE the base and the fake.
DEV_LABEL = _label(HOST, BASE, OVERRIDE, 'docker-compose.web.yaml', 'docker-compose.web.dev.yaml')
DEV_LABEL_DEVCONTAINER = _label(DEVCONTAINER, BASE, OVERRIDE, 'docker-compose.web.yaml', 'docker-compose.web.dev.yaml')
BARE_UP_LABEL = _label(HOST, BASE, OVERRIDE)
PROD_LABEL = _label(HOST, BASE, 'docker-compose.web.yaml')
SYSTEM_LABEL = _label(HOST, BASE, 'docker-compose.fake.yaml')

REASON_PROD = 'the running postgres was started without docker-compose.override.yaml'
REASON_NO_LABEL = 'the container carries no compose config_files label'
REASON_INSPECT_FAILED = "could not read the container's compose label"


def _malformed(label: str) -> str:
    return f'malformed label: {label}'


# ------------------------------------------------------------------------------------------------- the stub


STUB = r"""#! /bin/bash
# Stub docker for test_run_migrations_stack_selection.py. Answers from files in $STUB_DIR; journals argv.
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
    files=()
    while [ "${1:-}" = -f ]; do files+=("$2"); shift 2; done
    case "$1" in
        ps) answer "ps.$3" ;;
        config)
            for file in "${files[@]}"; do
                if [ "$file" = docker-compose.override.yaml ]; then echo trader_joe_data_store:dev; exit 0; fi
            done
            echo trader_joe_data_store:latest
            exit 0
            ;;
        run) answer run ;;
    esac
    exit 97
fi

if [ "$1" = inspect ]; then
    id="${!#}"
    if [ -f "$STUB_DIR/inspect.$id.rc" ]; then exit "$(cat "$STUB_DIR/inspect.$id.rc")"; fi
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


@dataclass(frozen=True)
class Stack:
    """What the stub reports. labels maps a container id to its label; None means `docker inspect` fails."""

    postgres: tuple[str, ...]
    labels: dict[str, str | None] = field(default_factory=dict)
    data_store: tuple[str, ...] = ()
    data_store_ps_fails: bool = False


@dataclass(frozen=True)
class Call:
    argv: tuple[str, ...]

    @property
    def is_compose(self) -> bool:
        return self.argv[:1] == ('compose',)

    @property
    def files(self) -> tuple[str, ...]:
        files, words = [], list(self.argv[1:])
        while words[:1] == ['-f']:
            files.append(words[1])
            words = words[2:]
        return tuple(files)

    @property
    def rest(self) -> tuple[str, ...]:
        return self.argv[1 + 2 * len(self.files) :]


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
        self.image_mode('match')

    def write(self, name: str, text: str) -> None:
        (self.dir / name).write_text(text, encoding='utf-8')

    def image_mode(self, mode: str) -> None:
        self.write('image_mode', mode)

    def stack(self, stack: Stack) -> None:
        self.write('ps.postgres', ''.join(f'{i}\n' for i in stack.postgres))
        self.write('ps.data_store', ''.join(f'{i}\n' for i in stack.data_store))
        if stack.data_store_ps_fails:
            self.write('ps.data_store.rc', '1')
        for container, label in stack.labels.items():
            if label is None:
                self.write(f'inspect.{container}.rc', '1')
            else:
                self.write(f'label.{container}', label)

    def run(self, *args: str, run_status: int = 0) -> Outcome:
        self.write('run', ALEMBIC_STDOUT + '\n')
        self.write('run.rc', str(run_status))
        if self.digest is not None:
            self.write('digest', self.digest + '\n')
        log = self.dir / 'calls.log'
        log.write_text('', encoding='utf-8')
        env = {k: v for k, v in os.environ.items() if k != 'MIGRATE_CHECK_ALLOW_STALE_IMAGE'}
        env |= {'PATH': f'{self.dir / "bin"}{os.pathsep}{os.environ["PATH"]}', 'STUB_DIR': str(self.dir)}
        result = subprocess.run(
            [BASH, str(SCRIPT), *args], capture_output=True, text=True, check=False, env=env, timeout=120
        )
        calls = [Call(tuple(line.split('\x1f')[:-1])) for line in log.read_text(encoding='utf-8').splitlines()]
        return Outcome(result, calls)


BASH = shutil.which('bash')


@pytest.fixture(scope='module')
def checkout_digest() -> str:
    """This checkout's source digest, so the stub can play an image whose stamp MATCHES and `check` reaches run."""
    result = subprocess.run(
        [str(REPO_ROOT / 'tools' / 'source_digest.sh'), 'data', 'store'],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, f'tools/source_digest.sh failed (gen/ is built by make test):\n{result.stderr}'
    return result.stdout.strip()


@pytest.fixture
def harness(tmp_path: Path, checkout_digest: str) -> Harness:
    assert BASH, 'bash is not on PATH, so the script cannot be exercised'
    return Harness(tmp_path, checkout_digest)


# ------------------------------------------------------------------------------------------------- the table

# (id, stack, the -f list expected, the stderr selection line expected)
CASES = [
    # --- production and other non-dev definitions: base alone, today's command
    ('prod', Stack(('pg',), {'pg': PROD_LABEL}), PROD_FILES, PROD_PREFIX + REASON_PROD),
    ('system-launch', Stack(('pg',), {'pg': SYSTEM_LABEL}), PROD_FILES, PROD_PREFIX + REASON_PROD),
    # --- unambiguous dev
    ('dev, dev-deps only', Stack(('pg',), {'pg': DEV_LABEL}), DEV_FILES, DEV_LINE),
    ('dev, devcontainer paths', Stack(('pg',), {'pg': DEV_LABEL_DEVCONTAINER}), DEV_FILES, DEV_LINE),
    ('dev, bare compose up', Stack(('pg',), {'pg': BARE_UP_LABEL}), DEV_FILES, DEV_LINE),
    (
        'dev postgres, dev data_store (each under its own root)',
        Stack(('pg',), {'pg': DEV_LABEL, 'ds': DEV_LABEL_DEVCONTAINER}, ('ds',)),
        DEV_FILES,
        DEV_LINE,
    ),
    # --- the label is absent, unreadable or malformed: base alone
    ('empty label', Stack(('pg',), {'pg': ''}), PROD_FILES, PROD_PREFIX + REASON_NO_LABEL),
    ('inspect fails', Stack(('pg',), {'pg': None}), PROD_FILES, PROD_PREFIX + REASON_INSPECT_FAILED),
    *[
        (name, Stack(('pg',), {'pg': label}), PROD_FILES, PROD_PREFIX + _malformed(label))
        for name, label in [
            ('leading comma', ',' + DEV_LABEL),
            ('trailing comma', DEV_LABEL + ','),
            ('double comma', f'{HOST}/{BASE},,{HOST}/{OVERRIDE}'),
            ('relative entry alone', OVERRIDE),
            ('relative override entry', f'{HOST}/{BASE},{OVERRIDE}'),
            ('space after a comma', f'{HOST}/{BASE}, {HOST}/{OVERRIDE}'),
        ]
    ],
    # --- near misses: well formed, but no entry's file name is exactly the override
    *[
        (name, Stack(('pg',), {'pg': label}), PROD_FILES, PROD_PREFIX + REASON_PROD)
        for name, label in [
            ('override .bak', _label(HOST, BASE, OVERRIDE + '.bak')),
            ('other-docker-compose.override.yaml', _label(HOST, BASE, 'other-' + OVERRIDE)),
            ('a directory named like the override', _label(HOST, BASE, f'{OVERRIDE}/{BASE}')),
        ]
    ],
    # --- more than one candidate container, or a stack the two containers disagree on: base alone
    (
        'two postgres containers',
        Stack(('pg1', 'pg2'), {'pg1': DEV_LABEL, 'pg2': DEV_LABEL}),
        PROD_FILES,
        PROD_PREFIX + '2 postgres containers in this project; cannot tell which stack',
    ),
    (
        'dev postgres, two data_store containers',
        Stack(('pg',), {'pg': DEV_LABEL, 'ds1': DEV_LABEL, 'ds2': DEV_LABEL}, ('ds1', 'ds2')),
        PROD_FILES,
        PROD_PREFIX + '2 data_store containers; cannot tell which stack',
    ),
    (
        'dev postgres, prod data_store',
        Stack(('pg',), {'pg': DEV_LABEL, 'ds': PROD_LABEL}, ('ds',)),
        PROD_FILES,
        PROD_PREFIX + 'postgres was started by the dev definition but data_store by PROD: mixed stack',
    ),
    (
        'dev postgres, data_store with no label',
        Stack(('pg',), {'pg': DEV_LABEL, 'ds': ''}, ('ds',)),
        PROD_FILES,
        PROD_PREFIX + f'postgres was started by the dev definition but data_store by UNKNOWN: {REASON_NO_LABEL}: '
        'mixed stack',
    ),
    (
        'dev postgres, data_store inspect fails',
        Stack(('pg',), {'pg': DEV_LABEL, 'ds': None}, ('ds',)),
        PROD_FILES,
        PROD_PREFIX + f'postgres was started by the dev definition but data_store by UNKNOWN: {REASON_INSPECT_FAILED}: '
        'mixed stack',
    ),
    (
        'dev postgres, data_store ps fails',
        Stack(('pg',), {'pg': DEV_LABEL}, data_store_ps_fails=True),
        PROD_FILES,
        PROD_PREFIX + 'could not list the data_store containers to cross-check the stack',
    ),
]

INVOCATIONS = [(), ('current',), ('history',), ('check',)]


def _alembic_args(invocation: tuple[str, ...]) -> tuple[str, ...]:
    return invocation or ('upgrade', 'head')


@pytest.mark.parametrize('invocation', INVOCATIONS, ids=lambda i: ' '.join(i) or 'no-args')
@pytest.mark.parametrize(('stack', 'files', 'line'), [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_the_running_stack_selects_the_definition_and_alembic_always_runs(
    harness: Harness, stack: Stack, files: tuple[str, ...], line: str, invocation: tuple[str, ...]
):
    harness.stack(stack)
    outcome = harness.run(*invocation)
    args = _alembic_args(invocation)

    assert outcome.result.returncode == 0, outcome.explain()

    # The command itself is unchanged; only the -f list varies. On PROD it is byte-identical to the old spelling.
    run = outcome.only('run')
    assert run.argv == ('compose', *[w for f in files for w in ('-f', f)], 'run', '--rm', '--no-deps', 'data_store',
                        ALEMBIC, *args), outcome.explain()  # fmt: skip

    # The postgres check before the selection is unchanged: base file alone.
    assert outcome.only('ps', '-q', 'postgres').files == PROD_FILES, outcome.explain()
    if 'check' in invocation:
        # The guard asks the SELECTED definition which image the one-off container will run.
        assert outcome.only('config', '--images', 'data_store').files == files, outcome.explain()
    else:
        assert not outcome.compose('config'), outcome.explain()

    # No file other than the base and the override is ever named, on any call.
    assert {f for call in outcome.calls if call.is_compose for f in call.files} <= {BASE, OVERRIDE}

    # Exactly one line on stderr, naming the definition and why.
    assert outcome.result.stderr.splitlines() == line.splitlines(), outcome.explain()
    assert outcome.result.stderr.count('run_migrations.sh: using the') == 1, outcome.explain()

    # Nothing added to stdout: the existing Running line, then alembic's own output (and, for check, the guard's
    # verified block, which predates this change).
    stdout = outcome.result.stdout.splitlines()
    assert stdout[0] == f"Running 'alembic {' '.join(args)}' against the revisions in {MIGRATION_DIR}", (
        outcome.explain()
    )
    assert stdout[-1] == ALEMBIC_STDOUT, outcome.explain()
    if 'check' in invocation:
        assert stdout[1].startswith('Comparison image verified'), outcome.explain()
    else:
        assert len(stdout) == 2, outcome.explain()
    assert 'definition' not in outcome.result.stdout, outcome.explain()


def test_a_newline_in_the_label_is_malformed_and_the_stack_stays_production(harness: Harness):
    """A newline in the label is malformed, and the stack stays on the production definition.

    Its reason echoes the label verbatim, so the one selection line spans two physical lines here -- the only
    case where it does (non-blocking finding on tj-s6r3rt.1).
    """
    label = f'{HOST}/{BASE}\n{HOST}/{OVERRIDE}'
    harness.stack(Stack(('pg',), {'pg': label}))
    outcome = harness.run('current')

    assert outcome.result.returncode == 0, outcome.explain()
    assert outcome.only('run').files == PROD_FILES, outcome.explain()
    assert outcome.result.stderr == PROD_PREFIX + _malformed(label) + '\n', outcome.explain()
    assert 'definition' not in outcome.result.stdout


@pytest.mark.parametrize(
    'stack',
    [
        Stack(('pg',), {'pg': DEV_LABEL}),
        Stack(('pg',), {'pg': PROD_LABEL}),
        Stack(('pg',), {'pg': None}),
        Stack(('pg1', 'pg2')),
        Stack(('pg',), {'pg': DEV_LABEL, 'ds': PROD_LABEL}, ('ds',)),
    ],
    ids=['dev', 'prod', 'inspect fails', 'two postgres', 'mixed'],
)
@pytest.mark.parametrize('status', [1, 3])
def test_the_exit_status_is_alembics_on_every_path(harness: Harness, stack: Stack, status: int):
    harness.stack(stack)
    outcome = harness.run('upgrade', 'head', run_status=status)
    assert outcome.result.returncode == status, outcome.explain()
    assert outcome.only('run'), outcome.explain()


def test_a_stopped_postgres_is_reported_before_any_selection(harness: Harness):
    """The postgres check precedes the selection (the guard's containment depends on that order)."""
    harness.stack(Stack(()))
    outcome = harness.run('check')

    assert outcome.result.returncode == 1, outcome.explain()
    assert 'postgres is not running' in outcome.result.stderr
    assert 'run_migrations.sh: using the' not in outcome.result.stderr
    assert [c.argv[0] for c in outcome.calls] == ['compose'], outcome.explain()
    assert not outcome.compose('run')


# ------------------------------------------------------------------------------------------------- the check guard


@pytest.mark.parametrize('image_mode', ['missing', 'unstamped', 'stale'])
@pytest.mark.parametrize(
    ('stack', 'files', 'build', 'image'),
    [
        (Stack(('pg',), {'pg': DEV_LABEL}), DEV_FILES, 'dev-build', 'trader_joe_data_store:dev'),
        (Stack(('pg',), {'pg': PROD_LABEL}), PROD_FILES, 'prod-build', 'trader_joe_data_store:latest'),
        (Stack(('pg',), {'pg': None}), PROD_FILES, 'prod-build', 'trader_joe_data_store:latest'),
        (
            Stack(('pg',), {'pg': DEV_LABEL, 'ds': PROD_LABEL}, ('ds',)),
            PROD_FILES,
            'prod-build',
            'trader_joe_data_store:latest',
        ),
    ],
    ids=['dev', 'prod', 'ambiguous', 'mixed'],
)
def test_the_check_guard_names_the_selected_image_and_its_build(
    harness: Harness, stack: Stack, files: tuple[str, ...], build: str, image: str, image_mode: str
):
    """tj-yb1bxj addendum 4: the refusal reads as before against the selected image; only FIX: follows the stack."""
    harness.stack(stack)
    harness.image_mode(image_mode)
    outcome = harness.run('check')

    assert outcome.result.returncode == 1, outcome.explain()
    assert not outcome.compose('run'), 'a refused check must never reach alembic'
    assert outcome.only('config', '--images', 'data_store').files == files, outcome.explain()
    stderr = outcome.result.stderr
    assert stderr.startswith('run_migrations.sh: using the'), outcome.explain()
    assert 'REFUSED' in stderr, outcome.explain()
    assert f'image reference: {image}' in stderr, outcome.explain()
    fixes = [line for line in stderr.splitlines() if line.startswith('FIX:')]
    assert fixes == [f'FIX: make {build}'], outcome.explain()


def test_upgrade_head_is_never_guarded_whatever_the_image(harness: Harness):
    """A remedy may not be refused (tj-yb1bxj addendum 3 item 6): no image lookup at all on upgrade."""
    harness.stack(Stack(('pg',), {'pg': DEV_LABEL}))
    harness.image_mode('missing')
    outcome = harness.run()
    assert outcome.result.returncode == 0, outcome.explain()
    assert not outcome.compose('config') and not [c for c in outcome.calls if c.argv[:1] == ('image',)]


# ------------------------------------------------------------------------------------------------- the classifier


def _classifier_block() -> str:
    lines = SCRIPT.read_text(encoding='utf-8').splitlines()
    begin = [i for i, line in enumerate(lines) if line.startswith('# BEGIN classify_config_files')]
    end = [i for i, line in enumerate(lines) if line.startswith('# END classify_config_files')]
    assert len(begin) == 1 and len(end) == 1 and begin[0] < end[0], (
        f'{SCRIPT.name} must mark classify_config_files with one BEGIN and one END line'
    )
    return '\n'.join(lines[begin[0] : end[0] + 1])


def _classify(label: str) -> str:
    result = subprocess.run(
        [BASH, '-c', f'set -euo pipefail\n{_classifier_block()}\nclassify_config_files "$1"', 'classify', label],
        capture_output=True,
        text=True,
        check=False,
        env={'PATH': os.environ['PATH']},
    )
    assert result.returncode == 0 and result.stderr == '', result
    return result.stdout


@pytest.mark.parametrize(
    ('label', 'verdict'),
    [
        (DEV_LABEL, 'DEV'),
        (DEV_LABEL_DEVCONTAINER, 'DEV'),
        (BARE_UP_LABEL, 'DEV'),
        (f'/{OVERRIDE}', 'DEV'),
        (_label(HOST, OVERRIDE, BASE), 'DEV'),
        (PROD_LABEL, 'PROD'),
        (SYSTEM_LABEL, 'PROD'),
        (f'{HOST}/{BASE}', 'PROD'),
        (_label(HOST, BASE, OVERRIDE + '.bak'), 'PROD'),
        (_label(HOST, BASE, 'other-' + OVERRIDE), 'PROD'),
        (_label(HOST, BASE, 'my-' + OVERRIDE), 'PROD'),
        (_label(HOST, BASE, 'docker-compose.override.yml'), 'PROD'),
        (_label(HOST, BASE, OVERRIDE.upper()), 'PROD'),
        (_label(HOST, BASE, f'{OVERRIDE}/{BASE}'), 'PROD'),
        (f'{HOST}/{BASE},{HOST}/', 'PROD'),
        ('', f'UNKNOWN: {REASON_NO_LABEL}'),
        (',' + DEV_LABEL, 'UNKNOWN: ' + _malformed(',' + DEV_LABEL)),
        (DEV_LABEL + ',', 'UNKNOWN: ' + _malformed(DEV_LABEL + ',')),
        (f'{HOST}/{BASE},,{HOST}/{OVERRIDE}', 'UNKNOWN: ' + _malformed(f'{HOST}/{BASE},,{HOST}/{OVERRIDE}')),
        (',', 'UNKNOWN: ' + _malformed(',')),
        (OVERRIDE, 'UNKNOWN: ' + _malformed(OVERRIDE)),
        (f'{HOST}/{BASE},{OVERRIDE}', 'UNKNOWN: ' + _malformed(f'{HOST}/{BASE},{OVERRIDE}')),
        (f'./{OVERRIDE}', 'UNKNOWN: ' + _malformed(f'./{OVERRIDE}')),
        (f'{HOST}/{BASE}, {HOST}/{OVERRIDE}', 'UNKNOWN: ' + _malformed(f'{HOST}/{BASE}, {HOST}/{OVERRIDE}')),
        (f'{HOST}/{BASE}\n{HOST}/{OVERRIDE}', 'UNKNOWN: ' + _malformed(f'{HOST}/{BASE}\n{HOST}/{OVERRIDE}')),
    ],
)
def test_the_classifier_reads_the_label_by_file_name_and_fails_closed(label: str, verdict: str):
    assert _classify(label) == verdict + '\n'
