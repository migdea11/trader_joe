"""build_infra pins for the Makefile's modes: the test data dir, and launching one mode tears down the other.

tj-iee6sn.18 (B9). Design: ADR tj-4rr0la addendum 26 -- (3) TEST and SYSTEM use the fixed literal
./volume_data/test, which the Makefile sets on the command line (it outranks the root env file), so
make system-launch and make seed-dump never use the owner's data dir; (4) system-launch gains dev-up's
down, after its disposable-database guard and before its up; dev-deps (and so dev-launch) and dev-tools
gain a prerequisite, leave-fake-mode, that downs the project ONLY when a running container of it
carries docker-compose.fake.yaml in its com.docker.compose.project.config_files label, by basename,
and does nothing otherwise; dev-up already downs unconditionally and gains nothing.

Docker-free, like every build_infra pin: make runs for real with `docker` stubbed first on PATH. The
stub records each call's argv and the variables the design cares about (DATA_DIR, the PGADMIN_*
placeholders, INSTANCE_WRITE_SECRET), answers `compose ... ps -q` with the container ids a case sets
and `inspect` with their labels. That compose's `ps` lists a fake-mode container when asked through
the DEV file set -- it lists by project label, and every fake-mode service is also declared in the
DEV set -- is the daemon's to show (the live sitting, tj-iee6sn.11), not this file's.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from common.tests.compose_model import BASE_FILE, FAKE_FILE, load
from common.tests.test_ci_invariants import (
    DEV_PROJECT,
    MAKEFILE,
    REPO_ROOT,
    SYSTEM_GUARD,
    _committed_dockerignore_rules,
    _compose_calls,
    _compose_projects,
    _expanded_make_variable,
    _is_excluded_from_context,
    _make_recipe,
    _run_make,
    _subprocess_env,
)
from common.tests.test_network_model import _make_prerequisites


pytestmark = pytest.mark.build_infra

LEAVE = 'leave-fake-mode'
SYSTEM_LAUNCH = 'system-launch'
# Spelled out, not read from the Makefile: the oracle must not be the file under test.
TEST_DATA_DIR = './volume_data/test'
GUARD_LINE = '$(SYSTEM_TEST_DISPOSABLE_GUARD)'
# dev-up's own down (addendum 26 (4): "the TOOLS_COMPOSE down with dev-down's placeholders").
DOWN_LINE = 'PGADMIN_EMAIL=unused PGADMIN_PASS=unused $(TOOLS_COMPOSE) down'
PLACEHOLDER = 'unused'
UNSET = '<unset>'
CONFIG_FILES_LABEL = 'com.docker.compose.project.config_files'
INSPECT_FORMAT = '{{index .Config.Labels "' + CONFIG_FILES_LABEL + '"}}'
ROOT_ENV_NAME = '.env'
ENV_TEMPLATE = REPO_ROOT / '.env.default'
SECRET_SCRIPT = REPO_ROOT / 'tools' / 'dev_instance_secret.sh'
WORKFLOW = REPO_ROOT / '.github' / 'workflows' / 'trader_joe_testing.yml'

# One record per call: the four variables, then argv, every field ended by \037 and the record by \036.
DOCKER_STUB = r"""#!/bin/sh
{
  printf '%s\037' "${DATA_DIR-<unset>}" "${PGADMIN_EMAIL-<unset>}" "${PGADMIN_PASS-<unset>}" \
    "${INSTANCE_WRITE_SECRET-<unset>}"
  for word in "$@"; do printf '%s\037' "$word"; done
  printf '\036'
} >> "$STUB_LOG"
if [ -n "${STUB_UNAVAILABLE:-}" ]; then
  echo 'Cannot connect to the Docker daemon at unix:///var/run/docker.sock.' >&2
  exit 1
fi
case "$1" in
  inspect) [ -n "${STUB_LABELS:-}" ] && printf '%s\n' "$STUB_LABELS"; exit "${STUB_INSPECT_EXIT:-0}" ;;
  image) [ "$2" = inspect ] && exit "${STUB_IMAGE_INSPECT_EXIT:-0}" ;;
esac
for word in "$@"; do
  if [ "$word" = ps ]; then [ -n "${STUB_PS:-}" ] && printf '%s\n' "$STUB_PS"; exit 0; fi
done
exit 0
"""

# Labels as compose writes them: the absolute path of every -f file, comma-joined, in -f order.
CHECKOUT = '/home/owner/trader_joe'
DEV_FILES = (
    'docker-compose.yaml',
    'docker-compose.override.yaml',
    'docker-compose.web.yaml',
    'docker-compose.web.dev.yaml',
)


def _label(*names: str, root: str = CHECKOUT) -> str:
    return ','.join(f'{root}/{name}' for name in names)


SYSTEM_LABEL = _label('docker-compose.yaml', 'docker-compose.fake.yaml')
TEST_LABEL = _label(*DEV_FILES, 'docker-compose.fake.yaml')
TEST_LABEL_FAKE_FIRST = _label('docker-compose.fake.yaml', *DEV_FILES)
TEST_LABEL_FAKE_MIDDLE = _label('docker-compose.yaml', 'docker-compose.fake.yaml', 'docker-compose.override.yaml')
DEV_LABEL = _label(*DEV_FILES)
TOOLS_LABEL = _label(*DEV_FILES, 'docker-compose.tools.yaml')
PROD_LABEL = _label('docker-compose.yaml', 'docker-compose.web.yaml')


# --- the stub and its log ---------------------------------------------------------------------------


class Call:
    def __init__(self, record: str):
        fields = record.split('\037')
        assert fields[-1] == '', record
        self.data_dir, self.pgadmin_email, self.pgadmin_pass, self.secret = fields[:4]
        self.argv = fields[4:-1]

    @property
    def line(self) -> str:
        return 'docker ' + ' '.join(self.argv)

    def compose(self) -> tuple[list[str], list[str]] | None:
        if self.argv[:1] != ['compose']:
            return None
        [call] = _compose_calls(self.line)
        return call

    def is_compose(self, subcommand: str) -> bool:
        call = self.compose()
        return call is not None and call[1][:1] == [subcommand]

    def __repr__(self) -> str:
        return f'Call(DATA_DIR={self.data_dir}, PGADMIN={self.pgadmin_email}/{self.pgadmin_pass}, argv={self.argv})'


def _stub_env(tmp_path: Path, **values: str | None) -> tuple[dict[str, str], Path]:
    stubs, log = tmp_path / 'stubs', tmp_path / 'docker.log'
    stubs.mkdir(exist_ok=True)
    (stubs / 'docker').write_text(DOCKER_STUB)
    (stubs / 'docker').chmod(0o755)
    # Nothing from the caller's shell may reach the recipe: these are exactly what the design pins.
    env = _subprocess_env(
        DATA_DIR=None, PGADMIN_EMAIL=None, PGADMIN_PASS=None, INSTANCE_WRITE_SECRET=None, COMPOSE_PROJECT_NAME=None
    )
    env.update(PATH=f'{stubs}:{os.environ["PATH"]}', STUB_LOG=str(log))
    for name, value in values.items():
        if value is None:
            env.pop(name, None)
        else:
            env[name] = value
    return env, log


def _calls(log: Path) -> list[Call]:
    if not log.exists():
        return []
    return [Call(record) for record in log.read_text(encoding='utf-8').split('\036') if record]


def _run(tmp_path: Path, target: str, *arguments: str, cwd: Path | None = None, **values: str | None):
    env, log = _stub_env(tmp_path, **values)
    result = _run_make(cwd or tmp_path, target, *arguments, env=env)
    return result, _calls(log)


def _downs(calls: list[Call]) -> list[Call]:
    return [call for call in calls if call.is_compose('down')]


def _files(variable: str) -> list[str]:
    [(files, rest)] = _compose_calls(_expanded_make_variable(variable, REPO_ROOT, _subprocess_env()))
    assert rest == [], (variable, rest)
    return files


def _assert_is_the_placeholder_down(call: Call) -> None:
    """dev-up's down: TOOLS_COMPOSE's files, a bare `down` (no -v, no --remove-orphans), on the placeholders."""
    files, rest = call.compose()
    assert files == _files('TOOLS_COMPOSE'), call
    # tj-grna9p.161 (ADR tj-4rr0la addendum 29 (2)): a dev/test teardown reaches ONLY trader_joe_dev.
    assert _compose_projects(call.line) == [DEV_PROJECT], call
    assert rest == ['down'], f'the down must be bare -- never -v or --remove-orphans (D2): {call}'
    assert (call.pgadmin_email, call.pgadmin_pass) == (PLACEHOLDER, PLACEHOLDER), call
    assert FAKE_FILE.name not in files, call


# --- (3) the test data dir --------------------------------------------------------------------------


def test_the_test_data_dir_is_the_fixed_literal():
    """Addendum 26 (3): 'TEST and SYSTEM use the fixed literal ./volume_data/test'."""
    assert _expanded_make_variable('TEST_DATA_DIR', REPO_ROOT, _subprocess_env()) == TEST_DATA_DIR


def test_system_compose_sets_data_dir_on_its_command_line_from_test_data_dir():
    """(3): the Makefile sets it on the command line, which outranks the root env file.

    The source spells $(TEST_DATA_DIR), so the one literal has one home; the expansion is exactly
    `DATA_DIR=./volume_data/test docker compose -f base -f fake`, the assignment in front of the call.
    """
    source = MAKEFILE.read_text(encoding='utf-8')
    [assignment] = re.findall(r'^SYSTEM_COMPOSE\s*:=\s*(.*)$', source, re.MULTILINE)
    assert assignment.startswith('DATA_DIR=$(TEST_DATA_DIR) docker compose '), assignment
    expanded = _expanded_make_variable('SYSTEM_COMPOSE', REPO_ROOT, _subprocess_env())
    assert expanded.split()[0] == f'DATA_DIR={TEST_DATA_DIR}', expanded
    assert _compose_calls(expanded) == [([BASE_FILE.name, FAKE_FILE.name], [])], expanded


def test_seed_dump_compose_carries_the_same_data_dir():
    """(3): seed-dump's compose line is SYSTEM_COMPOSE's, so its run never recreates postgres elsewhere."""
    expanded = _expanded_make_variable('SEED_DUMP_COMPOSE', REPO_ROOT, _subprocess_env())
    assert expanded.split()[0] == f'DATA_DIR={TEST_DATA_DIR}', expanded
    assert expanded.count('DATA_DIR=') == 1 and _compose_projects(expanded) == [DEV_PROJECT], expanded


def test_no_dev_or_prod_set_pins_data_dir():
    """(3): 'DEV keeps the owner's DATA_DIR' -- only the fake-mode sets carry the assignment."""
    for variable in ('PROD_COMPOSE', 'PROD_UP', 'DEV_COMPOSE', 'TOOLS_COMPOSE', 'TEST_CLIENT_COMPOSE'):
        expanded = _expanded_make_variable(variable, REPO_ROOT, _subprocess_env())
        assert 'DATA_DIR' not in expanded, (variable, expanded)


@pytest.mark.parametrize('relative', ['volume_data/test/postgres/PG_VERSION', 'volume_data/test/pgadmin/x'])
def test_the_test_data_dir_is_ignored_by_git_and_the_build_context(relative: str):
    """(3): 'inside the already ignored /volume_data/', and never sent in a build context either."""
    assert shutil.which('git'), 'git is not on PATH'
    ignored = subprocess.run(['git', '-C', str(REPO_ROOT), 'check-ignore', '-q', '--no-index', relative], check=False)
    assert ignored.returncode == 0, f'git does not ignore {relative}'
    assert _is_excluded_from_context(relative, _committed_dockerignore_rules()), relative


def test_the_test_data_dir_never_overlaps_the_owners_dev_data():
    """(3): it 'never overlaps ./volume_data/postgres or ./volume_data/pgadmin' (the compose fallback)."""
    test_dir = Path(TEST_DATA_DIR)
    for owner in (Path('./volume_data/postgres'), Path('./volume_data/pgadmin')):
        assert not test_dir.is_relative_to(owner) and not owner.is_relative_to(test_dir), owner


@pytest.mark.parametrize('caller', [None, '/srv/owner_data'])
def test_system_launch_up_runs_on_the_test_data_dir_whatever_the_caller_set(tmp_path: Path, caller: str | None):
    """(3), as make runs it: the up sees DATA_DIR=./volume_data/test, even under a caller's DATA_DIR."""
    result, calls = _run(tmp_path, SYSTEM_LAUNCH, **{SYSTEM_GUARD: '1', 'DATA_DIR': caller})
    assert result.returncode == 0, result.stderr
    [up] = [call for call in calls if call.is_compose('up')]
    assert up.data_dir == TEST_DATA_DIR, up
    # The placeholders are safe on a down ONLY (dev-down's comment): never on the up.
    assert (up.pgadmin_email, up.pgadmin_pass) == (UNSET, UNSET), up


# --- (4) system-launch: guard, then the down, then the up -------------------------------------------


def test_system_launch_recipe_is_guard_then_the_image_check_then_dev_ups_down_then_the_up():
    """(4): 'system-launch gains the same down', after the guard (the attestation covers the down too).

    tj-iee6sn.24 (the architect's 09:00 note, from tj-iee6sn.19's finding 3): between the guard and the
    down, the :test image check, so a refused run takes nothing down.
    """
    recipe = _make_recipe(SYSTEM_LAUNCH)
    assert len(recipe) == 4, recipe
    assert recipe[0] == GUARD_LINE, recipe
    assert recipe[1] == IMAGE_CHECK_LINE, recipe
    assert recipe[2] == DOWN_LINE == _make_recipe('dev-up')[0], recipe
    assert recipe[3].startswith('$(SYSTEM_COMPOSE) up '), recipe


def test_system_launch_runs_the_image_check_then_the_down_before_the_up(tmp_path: Path):
    """(4), as make runs it: the :test image check, the placeholder down, then the fake stack's up."""
    result, calls = _run(tmp_path, SYSTEM_LAUNCH, f'{SYSTEM_GUARD}=1')
    assert result.returncode == 0, result.stderr
    assert calls[0].argv == IMAGE_CHECK_ARGV, calls
    assert [call.compose()[1][0] for call in calls[1:]] == ['down', 'up'], calls
    _assert_is_the_placeholder_down(calls[1])
    assert calls[1].data_dir == UNSET, f'the down needs no DATA_DIR, and the up gets the test one: {calls}'
    assert calls[2].compose()[0] == [BASE_FILE.name, FAKE_FILE.name], calls


# --- tj-iee6sn.24: system-launch refuses without both :test images ----------------------------------

# Spelled out: the two refs docker-compose.fake.yaml names, in one inspect whose output is discarded.
TEST_IMAGES = ('trader_joe_data_store:test', 'trader_joe_data_ingest:test')
IMAGE_CHECK_ARGV = ['image', 'inspect', *TEST_IMAGES]
IMAGE_REFUSAL = (
    'make system-launch: the :test images do not exist; run make test-build first '
    '(compose would otherwise build them unstamped)'
)
IMAGE_CHECK_LINE = (
    'docker image inspect $(TEST_IMAGES) >/dev/null 2>&1 || { echo "' + IMAGE_REFUSAL + '" >&2; exit 1; }'
)


def test_test_images_is_the_two_refs_the_fake_overlay_names():
    assert _expanded_make_variable('TEST_IMAGES', REPO_ROOT, _subprocess_env()).split() == list(TEST_IMAGES)
    services = load(FAKE_FILE)['services']
    assert (services['data_store']['image'], services['data_ingest']['image']) == TEST_IMAGES


@pytest.mark.parametrize('exit_status', ['1', '125'])
def test_system_launch_refuses_without_the_test_images_before_the_down(tmp_path: Path, exit_status: str):
    """Either :test image absent: non-zero, naming make test-build, and NOTHING after the inspect.

    The architect's 09:00 note on tj-iee6sn.24: the one inspect fails, so no down and no up run; a
    refused run takes nothing down and compose never builds an absent image unstamped.
    """
    result, calls = _run(tmp_path, SYSTEM_LAUNCH, f'{SYSTEM_GUARD}=1', STUB_IMAGE_INSPECT_EXIT=exit_status)
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert IMAGE_REFUSAL in result.stderr, result.stderr
    assert [call.argv for call in calls] == [IMAGE_CHECK_ARGV], calls
    assert not _downs(calls) and not [call for call in calls if call.is_compose('up')], calls


def test_system_launch_refused_by_the_guard_never_checks_the_images(tmp_path: Path):
    """The guard stays first: without the attestation, not even the inspect runs."""
    result, calls = _run(tmp_path, SYSTEM_LAUNCH, STUB_IMAGE_INSPECT_EXIT='1', **{SYSTEM_GUARD: None})
    assert result.returncode != 0 and 'REFUSED' in result.stderr, result.stderr
    assert calls == [], calls
    assert IMAGE_REFUSAL not in result.stderr


def test_the_system_launch_comment_says_it_refuses_without_the_images():
    """The comment above system-launch says it refuses without the images, and where the refusal sits.

    The architect's 09:00 note: 'No build: it starts the :test images' alone was untrue.
    """
    text = MAKEFILE.read_text(encoding='utf-8')
    comment = text.split('.PHONY: system-launch', 1)[0].rsplit('# Behind the SAME disposable-database guard', 1)[1]
    flat = ' '.join(line.lstrip('# ').strip() for line in comment.splitlines())
    assert 'REFUSES when either is missing' in flat, flat
    assert 'after the guard and BEFORE the down' in flat, flat


def test_ci_tags_the_test_images_before_start_system_runs_make_system_launch():
    """The 09:00 note: 'CI is unaffected: it tags the pulled images :test before make system-launch'."""
    text = WORKFLOW.read_text(encoding='utf-8')
    start = text.index('- name: Start System')
    tags = [
        match.start()
        for match in re.finditer(r'^\s*docker tag .+ trader_joe_data_(?:store|ingest):test\s*$', text, re.MULTILINE)
    ]
    assert len(tags) == 2, tags
    assert all(tag < start for tag in tags), (tags, start)


def test_system_launchs_down_and_up_address_the_same_dev_project():
    """Review (1): the down and the up name one project, whatever the checkout is called.

    Re-pinned for tj-grna9p.161 (ADR tj-4rr0la addendum 29): both lines pass -p trader_joe_dev, so
    they name the dev/test project in any checkout and never the default project prod keeps.
    """
    tools = _expanded_make_variable('TOOLS_COMPOSE', REPO_ROOT, _subprocess_env())
    system = _expanded_make_variable('SYSTEM_COMPOSE', REPO_ROOT, _subprocess_env())
    assert _compose_projects(tools) == _compose_projects(system) == [DEV_PROJECT], (tools, system)
    assert _files('TOOLS_COMPOSE')[0] == _files('SYSTEM_COMPOSE')[0] == BASE_FILE.name


def test_the_guard_text_says_system_launch_takes_the_project_down():
    """(6): the attestation is the only barrier before the down on a shared host, so its text says so."""
    guard = MAKEFILE.read_text(encoding='utf-8').split('define SYSTEM_TEST_DISPOSABLE_GUARD', 1)[1]
    guard = guard.split('endef', 1)[0]
    assert 'production deployment' in guard and 'takes that project DOWN' in guard, guard
    assert TEST_DATA_DIR in guard, guard


# --- (4) leave-fake-mode: where it hangs ------------------------------------------------------------


@pytest.mark.parametrize('target', ['dev-deps', 'dev-tools'])
def test_leave_fake_mode_is_a_prerequisite_of_dev_deps_and_dev_tools(target: str):
    """(4): 'dev-deps (and so dev-launch) and dev-tools gain a prerequisite'."""
    assert LEAVE in _make_prerequisites(target), (target, _make_prerequisites(target))


def test_dev_launch_reaches_leave_fake_mode_through_dev_deps():
    assert 'dev-deps' in _make_prerequisites('dev-launch'), _make_prerequisites('dev-launch')


@pytest.mark.parametrize('target', ['dev-up', SYSTEM_LAUNCH, 'dev-down', 'prod-launch'])
def test_targets_that_down_unconditionally_or_never_take_no_leave_fake_mode(target: str):
    """(4): dev-up 'downs unconditionally and needs no prerequisite'.

    system-launch has its own down (and no prerequisite runs before its guard); dev-down downs
    anyway; prod-launch is unchanged (tj-oxxl8a).
    """
    assert LEAVE not in _make_prerequisites(target), (target, _make_prerequisites(target))


def test_leave_fake_mode_is_phony():
    phony = [line for line in MAKEFILE.read_text(encoding='utf-8').splitlines() if line.startswith('.PHONY:')]
    assert any(LEAVE in line.split(':', 1)[1].split() for line in phony)


@pytest.mark.parametrize(
    ('target', 'up'), [('dev-deps', ['up', '-d', 'postgres']), ('dev-tools', ['up', '-d', 'pgadmin'])]
)
def test_dev_deps_and_dev_tools_leave_fake_mode_before_their_up(tmp_path: Path, target: str, up: list[str]):
    """(4), as make runs it: with a fake container up, the down lands before the target's own up."""
    result, calls = _run(tmp_path, target, STUB_PS='c1', STUB_LABELS=SYSTEM_LABEL)
    assert result.returncode == 0, result.stderr
    composed = [call for call in calls if call.compose() is not None]
    subcommands = [call.compose()[1] for call in composed]
    assert [rest[0] for rest in subcommands] == ['ps', 'down', 'up'], calls
    _assert_is_the_placeholder_down(composed[1])
    assert subcommands[2] == up, calls


@pytest.mark.parametrize('target', ['dev-deps', 'dev-tools'])
def test_dev_deps_and_dev_tools_next_to_a_dev_stack_stay_an_in_place_up(tmp_path: Path, target: str):
    """(4): 'Otherwise it does nothing' -- a plain dev-deps beside a running dev stack downs nothing."""
    result, calls = _run(tmp_path, target, STUB_PS='c1\nc2', STUB_LABELS=f'{DEV_LABEL}\n{TOOLS_LABEL}')
    assert result.returncode == 0, result.stderr
    assert _downs(calls) == [], calls
    assert [call for call in calls if call.is_compose('up')], calls


def test_dev_up_downs_exactly_once_and_never_asks_for_the_mode(tmp_path: Path):
    """dev-up gains nothing: one down (its own), no ps/inspect, even with a fake container up."""
    result, calls = _run(tmp_path, 'dev-up', STUB_PS='c1', STUB_LABELS=SYSTEM_LABEL)
    assert result.returncode == 0, result.stderr
    assert len(_downs(calls)) == 1, calls
    assert not [call for call in calls if call.is_compose('ps') or call.argv[:1] == ['inspect']], calls


# --- (4) leave-fake-mode: what it does --------------------------------------------------------------


@pytest.mark.parametrize(
    ('ps', 'labels'),
    [
        ('c1', SYSTEM_LABEL),
        ('c1\nc2\nc3', '\n'.join([TEST_LABEL] * 3)),
        ('c1', TEST_LABEL_FAKE_FIRST),
        ('c1', TEST_LABEL_FAKE_MIDDLE),
        ('c1\nc2', f'{DEV_LABEL}\n{SYSTEM_LABEL}'),
        ('c1\nc2', f'\n{SYSTEM_LABEL}'),
        ('c1', _label('docker-compose.yaml', 'docker-compose.fake.yaml', root='/srv/some checkout')),
    ],
    ids=[
        'system',
        'test',
        'test-fake-first',
        'test-fake-middle',
        'mixed-dev-and-fake',
        'empty-and-fake',
        'spaced-path',
    ],
)
def test_a_running_fake_mode_container_makes_it_run_the_down(tmp_path: Path, ps: str, labels: str):
    """(4): a running container carrying the fake file, in any position, makes it down the project.

    'downs the project ... ONLY when a running container of the project carries
    docker-compose.fake.yaml in its config_files label' -- beside any other mode, too.
    """
    result, calls = _run(tmp_path, LEAVE, STUB_PS=ps, STUB_LABELS=labels)
    assert result.returncode == 0, result.stderr
    [down] = _downs(calls)
    _assert_is_the_placeholder_down(down)
    assert calls[-1] is down, f'the down is the last thing it does: {calls}'
    assert 'fake-mode container is up' in result.stdout, result.stdout


def test_it_asks_compose_for_the_dev_sets_running_containers_and_inspects_exactly_those(tmp_path: Path):
    """How the mode is read: DEV_COMPOSE ps -q, then one inspect of exactly those ids.

    ps -q lists running, non-one-off containers of the dev/test project: -p trader_joe_dev since
    tj-grna9p.161, so the lookup can never see prod's containers and the down that may follow reaches
    the same project. The inspect uses the fixed label format -- run_migrations.sh's basename rule.
    """
    result, calls = _run(tmp_path, LEAVE, STUB_PS='c1\nc2', STUB_LABELS=f'{DEV_LABEL}\n{DEV_LABEL}')
    assert result.returncode == 0, result.stderr
    ps, inspect = calls
    assert ps.compose() == (_files('DEV_COMPOSE'), ['ps', '-q']), ps
    assert _compose_projects(ps.line) == [DEV_PROJECT], ps
    assert inspect.argv == ['inspect', '--format', INSPECT_FORMAT, 'c1', 'c2'], inspect


@pytest.mark.parametrize(
    'labels',
    [
        DEV_LABEL,
        TOOLS_LABEL,
        PROD_LABEL,
        '',
        _label('docker-compose.yaml', 'docker-compose.fake.yaml.bak'),
        _label('docker-compose.yaml', 'my-docker-compose.fake.yaml'),
        _label('docker-compose.yaml', 'docker-compose.fake.yml'),
        _label('docker-compose.yaml', root='/srv/docker-compose.fake.yaml'),
        _label('docker-compose.yaml', root='/srv/docker-compose.fake.yaml.d'),
        f'{CHECKOUT}/docker-compose.fake.yaml/docker-compose.yaml',
    ],
    ids=[
        'dev',
        'dev-with-tools',
        'prod',
        'empty-label',
        'bak-file',
        'prefixed-file',
        'yml-not-yaml',
        'directory-named-like-it',
        'directory-containing-it',
        'directory-segment',
    ],
)
def test_anything_but_a_fake_file_by_basename_runs_no_down(tmp_path: Path, labels: str):
    """(4): 'and is a no-op otherwise'.

    A dev or prod container, an empty label, or a name that only contains the fake file's name (a
    .bak copy, a directory) never takes the project down.
    """
    result, calls = _run(tmp_path, LEAVE, STUB_PS='c1', STUB_LABELS=labels)
    assert result.returncode == 0, result.stderr
    assert _downs(calls) == [], calls


def test_nothing_up_runs_no_down_and_no_inspect(tmp_path: Path):
    result, calls = _run(tmp_path, LEAVE, STUB_PS=None, STUB_LABELS=SYSTEM_LABEL)
    assert result.returncode == 0, result.stderr
    assert [call.compose()[1] for call in calls] == [['ps', '-q']], calls


def test_a_container_gone_between_ps_and_inspect_still_lets_a_fake_one_decide(tmp_path: Path):
    """Inspect exits non-zero when one id has vanished, but still prints the others' labels."""
    result, calls = _run(tmp_path, LEAVE, STUB_PS='c1\nc2', STUB_LABELS=SYSTEM_LABEL, STUB_INSPECT_EXIT='1')
    assert result.returncode == 0, result.stderr
    assert len(_downs(calls)) == 1, calls


def test_an_unreachable_daemon_does_nothing_and_does_not_fail(tmp_path: Path):
    """Docker unavailable: ps fails, so there is no id, no inspect, no down, and dev-deps goes on."""
    result, calls = _run(tmp_path, LEAVE, STUB_UNAVAILABLE='1', STUB_PS='c1', STUB_LABELS=SYSTEM_LABEL)
    assert result.returncode == 0, result.stderr
    assert len(calls) == 1 and calls[0].is_compose('ps'), calls


def test_no_docker_on_path_does_nothing_and_does_not_fail(tmp_path: Path):
    """No docker binary at all: the recipe's ps comes back empty and it stops there, status 0."""
    env, _ = _stub_env(tmp_path)
    (tmp_path / 'stubs' / 'docker').unlink()
    path = [entry for entry in os.environ['PATH'].split(os.pathsep) if not (Path(entry) / 'docker').exists()]
    env['PATH'] = os.pathsep.join([str(tmp_path / 'stubs'), *path])
    result = _run_make(tmp_path, LEAVE, env=env)
    assert result.returncode == 0, result.stderr
    assert 'taking the project down' not in result.stdout, result.stdout


# --- CI: Start System's make system-launch, with the job's staged root env file -----------------------


def _ci_checkout(tmp_path: Path, secret_line: str | None) -> Path:
    """A directory shaped like the system job's checkout after Stage Pipeline Configs.

    The template copied to the root env file, its empty INSTANCE_WRITE_SECRET= line deleted and a
    value appended (or left as shipped).
    """
    work = tmp_path / 'work'
    (work / 'tools').mkdir(parents=True)
    shutil.copy(SECRET_SCRIPT, work / 'tools' / SECRET_SCRIPT.name)
    lines = ENV_TEMPLATE.read_text(encoding='utf-8').splitlines()
    if secret_line is not None:
        lines = [line for line in lines if not line.startswith('INSTANCE_WRITE_SECRET=')] + [secret_line]
    (work / ROOT_ENV_NAME).write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return work


def test_the_start_system_step_runs_make_system_launch_with_the_attestation_in_its_env():
    """CI's Start System is the target this bead changed: its down now runs on every system job."""
    text = WORKFLOW.read_text(encoding='utf-8')
    step = text.split('- name: Start System', 1)[1].split('- name:', 1)[0]
    assert re.search(rf'^\s+{SYSTEM_GUARD}:\s*"1"\s*$', step, re.MULTILINE), step
    assert re.search(r'^\s+make system-launch\s*$', step, re.MULTILINE), step


@pytest.mark.parametrize(
    ('secret_line', 'expected'),
    [('INSTANCE_WRITE_SECRET=ci-job-secret', 'ci-job-secret'), (None, 'unused')],
    ids=['staged-secret', 'template-empty-falls-back'],
)
def test_the_down_interpolates_with_the_ci_environment(tmp_path: Path, secret_line: str | None, expected: str):
    """The CI-shape check: the down interpolates in the system job's environment.

    No PGADMIN_*, no shell secret, the guard from the step env: the down gets the placeholders and
    an INSTANCE_WRITE_SECRET from tools/dev_instance_secret.sh -- the staged value, or 'unused' -- so
    neither :? guard can fire.
    """
    work = _ci_checkout(tmp_path, secret_line)
    result, calls = _run(tmp_path, SYSTEM_LAUNCH, cwd=work, **{SYSTEM_GUARD: '1'})
    assert result.returncode == 0, result.stderr
    image_check, down, up = calls
    assert image_check.argv == IMAGE_CHECK_ARGV, image_check
    _assert_is_the_placeholder_down(down)
    assert down.secret == expected, down
    assert up.is_compose('up') and up.data_dir == TEST_DATA_DIR, up


def test_make_n_system_launch_leaves_no_make_variable_unexpanded():
    """`make -n system-launch SYSTEM_TEST_DISPOSABLE_DB=1` leaves no make variable unexpanded.

    The only $( left is the shell's own command substitution of tools/dev_instance_secret.sh.
    """
    env = _subprocess_env(**{SYSTEM_GUARD: None})
    result = _run_make(REPO_ROOT, '-n', SYSTEM_LAUNCH, f'{SYSTEM_GUARD}=1', env=env)
    assert result.returncode == 0, result.stderr
    printed = result.stdout.replace('$(sh tools/dev_instance_secret.sh)', '')
    assert '$(' not in printed and '${' not in printed, result.stdout
    lines = [line for line in result.stdout.splitlines() if 'docker compose' in line]
    assert len(lines) == 2, result.stdout
    assert lines[0].startswith('PGADMIN_EMAIL=unused PGADMIN_PASS=unused INSTANCE_WRITE_SECRET='), lines
    assert lines[0].endswith(' -f docker-compose.tools.yaml down'), lines
    assert _compose_projects(lines[0]) == [DEV_PROJECT], lines
    assert (
        lines[1]
        == f'DATA_DIR={TEST_DATA_DIR} docker compose -p {DEV_PROJECT} -f docker-compose.yaml -f docker-compose.fake.yaml up -d --wait --wait-timeout 300'
    ), lines
