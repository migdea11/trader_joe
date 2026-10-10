"""The web chain's make targets and CI job, run for real against stand-ins (bead tj-grna9p.27).

Decision tj-grna9p.7 (option A): builder-shared owns the Makefile targets and CI that call the npm
scripts builder-ui defines in web/package.json. The rulings recorded on tj-grna9p.27:

* ONE NODE PIN. NODE_VERSION and its two checksums in the Makefile are the authority; `make
  node-install` is the only thing that downloads Node, sha256-verified, and CI installs it through that
  target, so the workflow holds no Node version and uses no setup-node.
* NOTHING GENERATED IS COMMITTED (user ruling 2026-10-06). The body's `git diff --exit-code` staleness
  step is superseded: CI regenerates gen/proto/ts (gen-proto-ts) and then type-checks.
* THE AUDIT (architect ruling). The full `npm audit` is REPORT-ONLY; `npm audit --omit=dev
  --audit-level=high` is the gate. `make security` stays Python-only and never runs npm.
* make lint's web leg, lint-ts, is selected by PATHS exactly as the proto leg is: '.', empty, 'web' or
  under web/. Unselected, it prints one 'not run' line and never looks for Node. Selected, a missing
  or wrong Node FAILS naming `make node-install`; never a skip.
* `make test PATHS=web` runs vitest and not pytest, with no venv; a Python-only PATHS never touches npm.

Every Makefile behaviour here is the recipe's, run from a scratch directory through the repository's
own Makefile with node, npm and uv stood in and the real buf filtered off PATH (test_make_lint.py's
Stubs). Recursive targets (web-check, test's web leg) need a Makefile in their working directory, so
those scratch directories hold a symlink to the real one. node-install fetches a fake release over
file://, so nothing is downloaded and nothing outside the test's directory is written.

NOT RUN here: the workflow on GitHub's runners, and a real npm, eslint, vue-tsc, vitest or audit.
"""

import io
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from common.tests.test_ci_invariants import (
    MAKEFILE,
    TESTING_WORKFLOW,
    _load_yaml,
    _make_recipe,
    _runs_make_target,
    _step_commands,
    _workflow_files,
)
from common.tests.test_make_lint import (
    CURL_WRAPPER,
    UNAME_STUB,
    WEB_LINT_CALLS,
    WEB_LINT_FIX_CALLS,
    Stubs,
    _calls,
    _clean_environment,
    _executable,
    _make,
    _path_without_buf,
    _pin,
    _ran,
    _sha256,
    web_calls_match,
)


pytestmark = pytest.mark.build_infra

WEB_TARGETS = (
    'web-install',
    'gen-proto-ts',
    'web-lint',
    'web-typecheck',
    'web-test',
    'web-build',
    'web-audit',
    'web-check',
    'node-install',
    'lint-ts',
    'lint-fix-ts',
)
# What web-check runs, in CI's order (install, generate, lint, typecheck, test, build, audit).
WEB_CHECK_CALLS = [
    ['--prefix', 'web', 'ci'],
    ['--prefix', 'web', 'run', 'gen:proto'],
    ['--prefix', 'web', 'run', 'lint'],
    ['--prefix', 'web', 'run', 'typecheck'],
    ['--prefix', 'web', 'run', 'test'],
    ['--prefix', 'web', 'run', 'build'],
    ['--prefix', 'web', 'audit'],
    ['--prefix', 'web', 'audit', '--omit=dev', '--audit-level=high'],
]
FULL_AUDIT = ['--prefix', 'web', 'audit']
AUDIT_GATE = ['--prefix', 'web', 'audit', '--omit=dev', '--audit-level=high']


@pytest.fixture
def stubs(tmp_path: Path) -> Stubs:
    return Stubs(tmp_path)


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    """A working directory with the repository's Makefile linked in (for $(MAKE) recursion) and an installed web/."""
    root = tmp_path / 'scratch'
    (root / 'web' / 'node_modules').mkdir(parents=True)
    (root / 'Makefile').symlink_to(MAKEFILE)
    return root


def _not_run_line(target: str, paths: str) -> str:
    return f'{target}: not run: PATHS={paths} does not cover web/ (eslint and vue-tsc run for PATHS=. or web/...)\n'


# ---------------------------------------------------------------------------------------------------
# THE TARGETS


@pytest.mark.parametrize('target', WEB_TARGETS)
def test_each_web_target_is_phony_and_documented(target: str):
    """.PHONY directly above the recipe (a file named like it would make it a no-op), and a ## help line."""
    lines = MAKEFILE.read_text(encoding='utf-8').splitlines()
    assert f'.PHONY: {target}' in lines, f'{target} is not declared .PHONY on a line of its own'
    header = [line for line in lines if re.match(rf'^{re.escape(target)}:(?!=)', line)]
    assert len(header) == 1 and re.search(r'\s##\s+\S', header[0]), f'{target} has no ## help line: {header}'
    assert lines[lines.index(f'.PHONY: {target}') + 1] == header[0], f'.PHONY: {target} is not directly above it'


def test_the_node_pin_is_one_version_and_two_checksums():
    version = _pin('NODE_VERSION')
    assert re.fullmatch(r'\d+\.\d+\.\d+', version), version
    sums = [_pin('NODE_SHA256_X86_64'), _pin('NODE_SHA256_AARCH64')]
    assert all(re.fullmatch(r'[0-9a-f]{64}', value) for value in sums) and sums[0] != sums[1], sums


def test_web_check_runs_the_chain_in_ci_order(stubs: Stubs, scratch: Path):
    stubs.install_node()
    result = _make(scratch, 'web-check', env=stubs.env())
    assert result.returncode == 0, _ran(result)
    assert stubs.npm_calls() == WEB_CHECK_CALLS, _ran(result)


def test_web_audit_reports_the_full_audit_without_failing(stubs: Stubs, scratch: Path):
    """A dev-only finding (the full audit failing) is printed and passes; the gate still runs after it."""
    stubs.install_node()
    result = _make(scratch, 'web-audit', env=stubs.env(NPM_STUB_FAIL_CALL=' '.join(FULL_AUDIT)))
    assert result.returncode == 0, _ran(result)
    assert stubs.npm_calls() == [FULL_AUDIT, AUDIT_GATE], _ran(result)


def test_web_audit_fails_on_a_high_runtime_finding(stubs: Stubs, scratch: Path):
    """--omit=dev --audit-level=high is the gate: its failure fails the target."""
    stubs.install_node()
    result = _make(scratch, 'web-audit', env=stubs.env(NPM_STUB_FAIL_CALL=' '.join(AUDIT_GATE)))
    assert result.returncode != 0, _ran(result)
    assert stubs.npm_calls() == [FULL_AUDIT, AUDIT_GATE], _ran(result)


def test_make_security_runs_no_npm():
    """The security target is Python-only, needs no Node, and is the identical line to CI's security job."""
    recipe = _make_recipe('security')
    assert not [line for line in recipe if re.search(r'\bnpm\b|\$\((WEB_)?NPM\)|web-audit', line)], recipe


# ---------------------------------------------------------------------------------------------------
# lint-ts: THE PATHS SELECTOR AND THE NODE CHECK

SELECTING_WEB = ['web', 'web/', './web', 'web/src', '.', './', '', 'server/common web', 'web server/common']
NOT_SELECTING_WEB = ['server/common', 'proto', 'server/data/store', 'tests', 'webapp', 'server/web', '../web']


@pytest.mark.parametrize('target', ['lint-ts', 'lint-fix-ts'])
@pytest.mark.parametrize('paths', NOT_SELECTING_WEB)
def test_a_scope_that_misses_web_prints_one_line_and_never_looks_for_node(
    stubs: Stubs, tmp_path: Path, target: str, paths: str
):
    """One line, exit 0, with Node absent and with it present: a component run behaves as before the UI."""
    absent = _make(tmp_path, target, f'PATHS={paths}', env=stubs.env())
    assert (absent.returncode, absent.stdout, absent.stderr) == (0, _not_run_line(target, paths), ''), _ran(absent)
    stubs.install_node()
    present = _make(tmp_path, target, f'PATHS={paths}', env=stubs.env())
    assert (present.returncode, present.stdout) == (0, _not_run_line(target, paths)), _ran(present)
    assert stubs.node_calls() == [] and stubs.npm_calls() == []


@pytest.mark.parametrize(('target', 'calls'), [('lint-ts', WEB_LINT_CALLS), ('lint-fix-ts', WEB_LINT_FIX_CALLS)])
@pytest.mark.parametrize('paths', SELECTING_WEB)
def test_a_scope_covering_web_runs_the_web_lint(
    stubs: Stubs, scratch: Path, target: str, calls: list[list[str]] | str, paths: str
):
    stubs.install_node()
    result = _make(scratch, target, f'PATHS={paths}', env=stubs.env())
    assert result.returncode == 0, _ran(result)
    assert web_calls_match(stubs.npm_calls(), calls), (stubs.npm_calls(), _ran(result))


@pytest.mark.parametrize(
    ('install', 'variables', 'named'),
    [(False, {}, 'node is not on PATH'), (True, {'NODE_STUB_VERSION': 'v22.0.0'}, 'found node v22.0.0 at')],
    ids=['no-node', 'another-version'],
)
@pytest.mark.parametrize('target', ['lint-ts', 'lint-fix-ts'])
@pytest.mark.parametrize('paths', ['web', '.'])
def test_a_scope_covering_web_fails_without_the_pinned_node_naming_the_remedy(
    stubs: Stubs, scratch: Path, target: str, paths: str, install: bool, variables: dict[str, str], named: str
):
    """Never a skip: the refusal names the pin and `make node-install`, and npm never runs."""
    if install:
        stubs.install_node()
    result = _make(scratch, target, f'PATHS={paths}', env=stubs.env(**variables))
    assert result.returncode != 0, _ran(result)
    for phrase in (named, f'needs node v{_pin("NODE_VERSION")}', 'make node-install'):
        assert phrase in result.stderr, f'the refusal does not say {phrase!r}:\n{_ran(result)}'
    assert stubs.npm_calls() == []


def test_a_scope_covering_web_without_node_modules_names_web_install(stubs: Stubs, tmp_path: Path):
    stubs.install_node()
    result = _make(tmp_path, 'lint-ts', 'PATHS=web', env=stubs.env())
    assert result.returncode != 0 and "run 'make web-install'" in result.stderr, _ran(result)
    assert stubs.npm_calls() == []


# ---------------------------------------------------------------------------------------------------
# make test: web words go to vitest, the rest to pytest


@pytest.mark.parametrize('paths', ['web', 'web/src', './web'])
def test_make_test_on_web_runs_vitest_and_not_pytest_with_no_venv(stubs: Stubs, scratch: Path, paths: str):
    """The scratch directory has no pyproject.toml: a venv or proto prerequisite would fail the run."""
    stubs.install_node().install_uv()
    result = _make(scratch, 'test', f'PATHS={paths}', env=stubs.env())
    assert result.returncode == 0, _ran(result)
    assert stubs.npm_calls() == [['--prefix', 'web', 'run', 'test']], _ran(result)
    assert stubs.uv_calls() == [], 'a web-only PATHS reached uv'
    assert 'pytest not run' in result.stdout, _ran(result)
    assert not (scratch / '.venv').exists()


@pytest.mark.parametrize(
    ('paths', 'pytest_words', 'npm'),
    [
        ('server/common', ['server/common'], []),
        ('server/common tools', ['server/common', 'tools'], []),
        ('server/common web', ['server/common'], [['--prefix', 'web', 'run', 'test']]),
    ],
    ids=['python-only', 'two-python-words', 'mixed'],
)
def test_make_test_runs_pytest_on_the_python_words_and_npm_only_for_web(
    stubs: Stubs, scratch: Path, paths: str, pytest_words: list[str], npm: list[list[str]]
):
    stubs.install_node().install_uv()
    env = stubs.env()
    marker = _pin('VENV_MARKER')
    result = _make(scratch, '-o', marker, '-o', 'proto', 'test', f'PATHS={paths}', env=env)
    assert result.returncode == 0, _ran(result)
    assert stubs.uv_calls() == [['run', 'pytest', *pytest_words]], _ran(result)
    assert stubs.npm_calls() == npm, _ran(result)
    if not npm:
        assert stubs.node_calls() == [], 'a Python-only PATHS looked for node'


# ---------------------------------------------------------------------------------------------------
# make node-install, against a fake release served over file://


class NodeRelease:
    """A fake nodejs.org tree, a uname reporting any platform, and install directories nothing else uses."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / 'stub-bin'
        self.bin.mkdir()
        self.install_dir = root / 'install' / 'bin'
        self.home_dir = root / 'install' / 'lib'
        self.version = _pin('NODE_VERSION')
        self.release = root / 'release'
        (self.release / f'v{self.version}').mkdir(parents=True)
        self.curl_log = root / 'curl.calls'
        path = _path_without_buf()
        curl = shutil.which('curl', path=path)
        assert curl, 'curl is not on PATH, so node-install cannot fetch even a file:// release'
        _executable(self.bin / 'uname', UNAME_STUB)
        _executable(self.bin / 'curl', CURL_WRAPPER.format(curl=curl))
        self.path = os.pathsep.join([str(self.bin), path])

    def tarball(self, arch: str, reports: str | None = None) -> bytes:
        """A release tarball for ARCH whose node prints REPORTS (the pinned version by default)."""
        name = f'node-v{self.version}-linux-{arch}'
        scripts = {
            'node': f'#!/bin/sh\necho {reports or "v" + self.version}\n',
            'npm': '#!/bin/sh\necho npm\n',
            'npx': '#!/bin/sh\necho npx\n',
        }
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
            for tool, text in scripts.items():
                data = text.encode()
                info = tarfile.TarInfo(f'{name}/bin/{tool}')
                info.size, info.mode = len(data), 0o755
                archive.addfile(info, io.BytesIO(data))
        data = buffer.getvalue()
        (self.release / f'v{self.version}' / f'{name}.tar.gz').write_bytes(data)
        return data

    def install(self, *arguments: str, platform: tuple[str, str] = ('Linux', 'x86_64')):
        env = _clean_environment() | {
            'PATH': self.path,
            'HOME': str(self.root / 'home'),
            'UNAME_S': platform[0],
            'UNAME_M': platform[1],
            'CURL_LOG': str(self.curl_log),
        }
        return _make(
            self.root,
            'node-install',
            f'NODE_RELEASE_URL={self.release.as_uri()}',
            f'NODE_INSTALL_DIR={self.install_dir}',
            f'NODE_HOME_DIR={self.home_dir}',
            *arguments,
            env=env,
        )

    def installed(self) -> dict[str, list[str]]:
        """What the two install directories hold, hidden temporaries included."""
        return {
            'bin': sorted(os.listdir(self.install_dir)) if self.install_dir.exists() else [],
            'lib': sorted(os.listdir(self.home_dir)) if self.home_dir.exists() else [],
        }

    def curl_calls(self) -> list[list[str]]:
        return _calls(self.curl_log)


@pytest.fixture
def node_release(tmp_path: Path) -> NodeRelease:
    return NodeRelease(tmp_path)


@pytest.mark.parametrize(
    ('machine', 'arch', 'checksum'),
    [('x86_64', 'x64', 'NODE_SHA256_X86_64'), ('aarch64', 'arm64', 'NODE_SHA256_AARCH64')],
)
def test_a_release_matching_the_pin_is_installed_and_runs(
    node_release: NodeRelease, machine: str, arch: str, checksum: str
):
    """The control for the refusals: verified, unpacked under NODE_HOME_DIR, node/npm/npx linked and runnable."""
    data = node_release.tarball(arch)
    result = node_release.install(f'{checksum}={_sha256(data)}', platform=('Linux', machine))
    assert result.returncode == 0, _ran(result)
    name = f'node-v{node_release.version}-linux-{arch}'
    assert node_release.installed() == {'bin': ['node', 'npm', 'npx'], 'lib': [name]}
    ran = subprocess.run(
        [str(node_release.install_dir / 'node'), '--version'], capture_output=True, text=True, check=False
    )
    assert ran.stdout.strip() == f'v{node_release.version}', ran
    ((url,),) = [[call[-1]] for call in node_release.curl_calls()]
    assert url == f'{node_release.release.as_uri()}/v{node_release.version}/{name}.tar.gz'


def test_a_checksum_mismatch_is_refused_and_installs_nothing(node_release: NodeRelease):
    """The default pin cannot match the fake tarball: refused, the download deleted, nothing linked."""
    data = node_release.tarball('x64')
    result = node_release.install()
    assert result.returncode != 0, _ran(result)
    assert 'checksum mismatch' in result.stderr, _ran(result)
    assert f'expected {_pin("NODE_SHA256_X86_64")}, got {_sha256(data)}' in result.stderr, _ran(result)
    assert node_release.installed() == {'bin': [], 'lib': []}


def test_a_crossed_checksum_is_refused(node_release: NodeRelease):
    """aarch64 is checked against its own checksum, never x86_64's."""
    data = node_release.tarball('arm64')
    result = node_release.install(f'NODE_SHA256_X86_64={_sha256(data)}', platform=('Linux', 'aarch64'))
    assert result.returncode != 0 and 'checksum mismatch' in result.stderr, _ran(result)
    assert node_release.installed() == {'bin': [], 'lib': []}


def test_a_release_reporting_another_version_is_refused(node_release: NodeRelease):
    data = node_release.tarball('x64', reports='v1.0.0')
    result = node_release.install(f'NODE_SHA256_X86_64={_sha256(data)}')
    assert result.returncode != 0, _ran(result)
    assert 'reports version v1.0.0' in result.stderr, _ran(result)
    assert node_release.installed() == {'bin': [], 'lib': []}


@pytest.mark.parametrize('platform', [('Darwin', 'arm64'), ('Linux', 'riscv64')], ids='-'.join)
def test_an_unpinned_platform_fails_before_any_download(node_release: NodeRelease, platform: tuple[str, str]):
    result = node_release.install(platform=platform)
    assert result.returncode != 0, _ran(result)
    assert f'no node checksum pinned for {platform[0]} {platform[1]}' in result.stderr, _ran(result)
    assert node_release.curl_calls() == []


# ---------------------------------------------------------------------------------------------------
# THE WORKFLOW


def _jobs() -> dict:
    return _load_yaml(TESTING_WORKFLOW)['jobs']


def _make_targets(job: dict) -> list[str]:
    """The make target each step runs, in step order (one per make command)."""
    return [
        word
        for step in job.get('steps') or []
        for command in _step_commands(step)
        if command and Path(command[0]).name == 'make'
        for word in command[1:2]
    ]


def test_the_web_job_runs_the_chain_through_make_in_order():
    """gen-proto-ts before web-typecheck: the TS is regenerated from proto/, then checked (no staleness diff)."""
    assert _make_targets(_jobs()['web']) == [
        'buf-install',
        'node-install',
        'web-install',
        'gen-proto-ts',
        'web-lint',
        'web-typecheck',
        'web-test',
        'web-build',
        'web-audit',
    ]


def test_the_web_job_can_only_read_the_repository():
    assert _jobs()['web']['permissions'] == {'contents': 'read'}


@pytest.mark.parametrize('job_id', ['web', 'testing'])
def test_node_reaches_the_job_only_through_make_node_install(job_id: str):
    """One step runs make node-install; nothing else installs Node, and it precedes every web target."""
    job = _jobs()[job_id]
    steps = job['steps']
    installs = [
        index for index, step in enumerate(steps) for c in _step_commands(step) if _runs_make_target(c, 'node-install')
    ]
    assert len(installs) == 1, installs
    for step in steps:
        assert 'setup-node' not in str(step.get('uses') or ''), step
        assert not re.search(r'nodejs\.org|\bnvm\b|apt(-get)? install[^\n]*\bnodejs\b', str(step.get('run') or '')), (
            step
        )
    web_steps = [
        index
        for index, step in enumerate(steps)
        for command in _step_commands(step)
        if any(_runs_make_target(command, target) for target in ('web-install', 'lint', *WEB_TARGETS[1:8]))
    ]
    assert web_steps and min(web_steps) > installs[0], (installs, web_steps)


def test_the_workflows_hold_no_node_version_literal():
    """NODE_VERSION has one definition, the Makefile's."""
    version = _pin('NODE_VERSION')
    for workflow in _workflow_files():
        text = workflow.read_text(encoding='utf-8')
        assert version not in text and 'node-version' not in text, workflow.name


def test_no_workflow_diffs_generated_or_web_sources():
    """The superseded staleness step: nothing generated is committed, so no git diff over gen/ or web/."""
    for workflow in _workflow_files():
        for job in (_load_yaml(workflow).get('jobs') or {}).values():
            for step in job.get('steps') or []:
                for command in _step_commands(step):
                    if command[:2] == ['git', 'diff']:
                        assert not [w for w in command[2:] if w.lstrip('./').startswith(('gen', 'web'))], command


def test_every_uses_is_pinned_to_a_full_commit_sha():
    """The workflow's own RULE (tj-cg2i9p): a tag or branch can be moved under the job."""
    unpinned = []
    for workflow in _workflow_files():
        for job in (_load_yaml(workflow).get('jobs') or {}).values():
            for step in job.get('steps') or []:
                uses = step.get('uses')
                if uses and not uses.startswith('./') and not re.fullmatch(r'[\w.-]+/[\w./-]+@[0-9a-f]{40}', uses):
                    unpinned.append(f'{workflow.name}: {uses}')
    assert not unpinned, unpinned


# ---------------------------------------------------------------------------------------------------
# WITH THE REAL npm: WHERE eslint RUNS
#
# The stand-in npm above records arguments, and npm's own semantics decide what they mean: `npm run`
# runs a script in the package directory, `npm exec` in the CALLER's working directory, --prefix or
# not. eslint finds its flat config from its working directory, so a leg whose eslint runs in the
# repository root finds no eslint.config.* and fails. These run the REAL pinned npm (offline: nothing
# is fetched) against a probe package whose eslint is a stand-in recording where it ran. Absent or at
# another version, they FAIL, never skip (pytest.ini): `make node-install`.

FAKE_ESLINT = '#!/bin/sh\nprintf \'%s|%s\\n\' "$(pwd)" "$*" >> "$ESLINT_LOG"\n'


@pytest.fixture(scope='module')
def real_node() -> str:
    pinned = f'v{_pin("NODE_VERSION")}'
    found = shutil.which('node')
    assert found and shutil.which('npm'), (
        f'node and npm are not on PATH. These tests run the pinned node {pinned}: install it with '
        f'`make node-install`, or rebuild the agent image. Never a skip.'
    )
    version = subprocess.run([found, '--version'], capture_output=True, text=True, check=False).stdout.strip()
    assert version == pinned, f'{found} is node {version!r}, not the pinned {pinned}: run `make node-install`'
    return found


@pytest.mark.parametrize(('target', 'fix'), [('lint-ts', False), ('lint-fix-ts', True)])
def test_eslint_runs_inside_web_where_its_config_is(
    real_node: str, scratch: Path, tmp_path: Path, target: str, fix: bool
):
    """Both legs reach eslint with web/ as its working directory; lint-fix-ts passes --fix, lint-ts does not."""
    web = scratch / 'web'
    (web / 'package.json').write_text(
        '{"name": "probe", "version": "0.0.0", "private": true,'
        ' "scripts": {"lint": "eslint .", "typecheck": "node --version"}}\n',
        encoding='utf-8',
    )
    (web / 'node_modules' / '.bin').mkdir()
    _executable(web / 'node_modules' / '.bin' / 'eslint', FAKE_ESLINT)
    log = tmp_path / 'eslint.calls'
    env = _clean_environment() | {
        'ESLINT_LOG': str(log),
        'npm_config_offline': 'true',
        'npm_config_update_notifier': 'false',
        'npm_config_cache': str(tmp_path / 'npm-cache'),
    }
    result = _make(scratch, target, 'PATHS=web', env=env)
    assert result.returncode == 0, _ran(result)
    calls = log.read_text(encoding='utf-8').splitlines() if log.exists() else []
    assert len(calls) == 1, (calls, _ran(result))
    cwd, _, arguments = calls[0].partition('|')
    assert Path(cwd) == web, f'eslint ran in {cwd}, not {web}: it finds no eslint.config there'
    assert ('--fix' in arguments.split()) == fix, arguments
