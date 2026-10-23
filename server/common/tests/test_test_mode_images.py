"""build_infra pins for the test-mode images: the :test tags, make test-build, and CI on :test.

tj-iee6sn.19 (B10). Design: ADR tj-4rr0la addendum 27 (1)-(2), the owner's answer on tj-iee6sn.22
(Q1 = prod images + fake broker; tag :test so :latest is never touched), and the architect's 20:45
and 22:50 UTC notes on tj-iee6sn.19. Test mode is the base file plus docker-compose.fake.yaml (the
test-mode overlay), which names the test pair trader_joe_data_store:test and
trader_joe_data_ingest:test. `make test-build` builds exactly those, from the prod_image target,
stamped like prod-build; prod-build, prod-launch and PROD_COMPOSE keep :latest and never load the
overlay. CI's System Testing job tags the images it pulled as :test and migrates through the test
pair; its deploy build and push stay on :latest.

The overlay's own key set and the merged system stack are pinned in test_fake_overlay.py; the
System Testing job's compose spellings in test_ci_invariants.py. Docker-free, like every
build_infra pin: make runs for real with `docker` stubbed first on PATH.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

from common.tests.compose_model import BASE_FILE, FAKE_FILE, load, prod_model
from common.tests.test_ci_invariants import (
    MAKEFILE,
    REPO_ROOT,
    SYSTEM_GUARD,
    TESTING_WORKFLOW,
    _compose_calls,
    _expanded_make_variable,
    _load_yaml,
    _make_recipe,
    _run_make,
    _step_lines,
    _subprocess_env,
    _system_job,
    _system_step,
)


pytestmark = pytest.mark.build_infra

TEST_BUILD = 'test-build'
# Spelled out, not read from the overlay: the oracle must not be the file under test.
TEST_IMAGES = {'data_store': 'trader_joe_data_store:test', 'data_ingest': 'trader_joe_data_ingest:test'}
PROD_IMAGES = {'data_store': 'trader_joe_data_store:latest', 'data_ingest': 'trader_joe_data_ingest:latest'}
TEST_DATA_DIR = './volume_data/test'
# The stamp's env names, read from what the build actually consumes: the base file's build args.
_STAMP_ARG = re.compile(r'^\$\{(SOURCE_DIGEST_\w+):-\}$')
# A :test IMAGE tag, in either spelling a file could carry one: a trader_joe image name, or any
# compose `image:` value. Prose about ":test" (comments that name no image) does not match.
_TEST_TAG = re.compile(r"\btrader_joe_[a-z_]+:test\b|\bimage:\s*[\"']?[^\s\"']+:test\b")
# The one place a :test tag may be WRITTEN besides the overlay: System Testing's Pull Docker Images
# step, which tags the images it pulled (addendum 27 (2), 'docker tag the :latest it already has').
PULL_STEP = 'Pull Docker Images'
START_STEP = 'Start System'
MIGRATE_STEP = 'Migrate Database'
BUILD_JOB = 'build'
GUARD_LINE = '(make system-launch, then make migrate)'

DOCKER_STUB = """#!/bin/sh
printf '%s|%s|%s\\n' "${SOURCE_DIGEST_DATA_STORE-UNSET}" "${SOURCE_DIGEST_DATA_INGEST-UNSET}" "$*" >> "$STUB_LOG"
exit 0
"""


def _stub_docker(tmp_path: Path, **overrides: str | None) -> tuple[dict[str, str], Path]:
    stubs, log = tmp_path / 'stubs', tmp_path / 'docker.log'
    stubs.mkdir()
    (stubs / 'docker').write_text(DOCKER_STUB)
    (stubs / 'docker').chmod(0o755)
    env = _subprocess_env(**overrides)
    env.update(PATH=f'{stubs}:{os.environ["PATH"]}', STUB_LOG=str(log))
    return env, log


def _docker_calls(log: Path) -> list[tuple[str, str, str]]:
    lines = log.read_text(encoding='utf-8').splitlines() if log.exists() else []
    return [tuple(line.split('|', 2)) for line in lines]


def _dry_run(target: str, *skipped: str) -> list[str]:
    """`make -n <target>` from the repository root, prerequisites in `skipped` treated as up to date."""
    options = [word for name in skipped for word in ('-o', name)]
    result = _run_make(REPO_ROOT, '-n', *options, target, env=_subprocess_env(**{SYSTEM_GUARD: None}))
    assert result.returncode == 0, f'`make -n {target}` failed: {result.stderr}'
    folded = re.sub(r'\\\n\t?', ' ', result.stdout)
    return [line.strip() for line in folded.splitlines() if line.strip()]


def _prerequisites(target: str) -> list[str]:
    header = next(line for line in MAKEFILE.read_text(encoding='utf-8').splitlines() if line.startswith(f'{target}:'))
    return header.split(':', 1)[1].split('#')[0].split()


def _stamp_names() -> set[str]:
    """The SOURCE_DIGEST_* variables the base file's data_store and data_ingest builds read."""
    names = set()
    for service in TEST_IMAGES:
        match = _STAMP_ARG.match(load(BASE_FILE)['services'][service]['build']['args']['SOURCE_DIGEST'])
        assert match, f'{service} build arg SOURCE_DIGEST is not a ${{SOURCE_DIGEST_*:-}} interpolation'
        names.add(match.group(1))
    assert len(names) == len(TEST_IMAGES), f'data_store and data_ingest share a stamp variable: {names}'
    return names


def _exported_stamp(lines: list[str]) -> set[str]:
    exports = {
        word for line in lines for match in re.finditer(r'\bexport ([\w ]+);', line) for word in match.group(1).split()
    }
    return {name for name in exports if name.startswith('SOURCE_DIGEST_')}


# --- the overlay is the one file that names a :test image ------------------------------------------


def test_the_overlay_names_exactly_the_two_test_images():
    """Addendum 27 (2): data_store and data_ingest :test, nothing else tagged, nothing tagged twice."""
    services = load(FAKE_FILE)['services']
    tagged = {name: spec['image'] for name, spec in services.items() if 'image' in spec}
    assert tagged == TEST_IMAGES, tagged


def test_prod_keeps_latest_and_never_loads_the_overlay():
    """prod-build, prod-launch and PROD_COMPOSE: :latest untouched, the overlay never read."""
    images = {name: prod_model()['services'][name]['image'] for name in TEST_IMAGES}
    assert images == PROD_IMAGES, images
    env = _subprocess_env()
    for variable in ('PROD_COMPOSE', 'PROD_UP'):
        expanded = _expanded_make_variable(variable, REPO_ROOT, env)
        assert FAKE_FILE.name not in expanded, f'{variable} ({expanded}) loads {FAKE_FILE.name}'
    for target in ('prod-build', 'prod-launch'):
        lines = _dry_run(target, 'proto', 'gen-proto-ts', 'prod-deps')
        calls = [call for line in lines for call in _compose_calls(line)]
        assert calls, f'`make -n {target}` runs no docker compose: {lines}'
        assert all(FAKE_FILE.name not in files for files, _ in calls), (target, calls)


def _tracked_files() -> list[str]:
    result = subprocess.run(['git', 'ls-files'], cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    return result.stdout.splitlines()


def _is_test_or_doc(path: str) -> bool:
    parts = Path(path).parts
    return 'tests' in parts or path.endswith(('.spec.ts', '.md'))


def test_no_file_but_the_overlay_and_the_ci_pull_step_names_a_test_image():
    """The overlay's header: its image: keys are the only place that names a :test tag.

    Every tracked file outside the test and doc trees -- the Makefile, every other compose file,
    tools/, the Dockerfiles, the workflow's build and push steps -- is scanned. The two allowed
    writers are the overlay itself and CI's Pull Docker Images step, which tags what it pulled.

    tj-iee6sn.24 adds two READERS, each one line and nothing more: the Makefile's TEST_IMAGES (the
    refs system-launch's presence check inspects) and stack.py's TEST_IMAGES (the refs test_up inspects
    and reads the stamps of). Neither names a ref any build or tag step writes; see the pins below.
    """
    pull_lines = set(_step_lines(_system_step(PULL_STEP)))
    readers = {
        'Makefile': 'TEST_IMAGES := trader_joe_data_store:test trader_joe_data_ingest:test',
        'tools/agent_mcp/stack.py': "TEST_IMAGES = ('trader_joe_data_store:test', 'trader_joe_data_ingest:test')",
    }
    offenders = []
    for path in _tracked_files():
        if path == FAKE_FILE.name or _is_test_or_doc(path):
            continue
        file = REPO_ROOT / path
        try:
            text = file.read_text(encoding='utf-8')
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if not _TEST_TAG.search(line):
                continue
            if file == TESTING_WORKFLOW and line.strip() in pull_lines:
                continue
            if readers.get(path) == line.strip():
                continue
            offenders.append(f'{path}:{number}: {line.strip()}')
    assert offenders == [], f'a :test image tag outside the test-mode overlay: {offenders}'
    for path, line in readers.items():
        lines = (REPO_ROOT / path).read_text(encoding='utf-8').splitlines()
        assert lines.count(line) == 1, (path, line)


def test_the_makefiles_test_images_are_only_ever_inspected():
    """The Makefile reader is read-only: $(TEST_IMAGES) is in system-launch's inspect and nowhere else.

    No build, tag, push or pull names it.
    """
    uses = [line.strip() for line in MAKEFILE.read_text(encoding='utf-8').splitlines() if '$(TEST_IMAGES)' in line]
    assert len(uses) == 1 and uses[0].lstrip('@').startswith('docker image inspect $(TEST_IMAGES) >/dev/null'), uses


# --- make test-build ------------------------------------------------------------------------------


def test_test_build_is_the_stamp_then_the_test_pair_build_and_nothing_else():
    """Addendum 27 (2): $(WITH_SOURCE_STAMP) $(SYSTEM_COMPOSE) build data_store data_ingest.

    The source recipe is exactly that one line, and as make expands it: the stamp, then ONE compose
    call -- DATA_DIR=./volume_data/test, base then the overlay, `build data_store data_ingest` --
    never PROD_COMPOSE's set (the web file) and no other service.
    """
    assert _make_recipe(TEST_BUILD) == ['$(WITH_SOURCE_STAMP) $(SYSTEM_COMPOSE) build data_store data_ingest']
    lines = _dry_run(TEST_BUILD, 'proto')
    assert len(lines) == 1, f'`make -n {TEST_BUILD}` runs {lines}'
    [line] = lines
    assert _exported_stamp(lines) == _stamp_names(), line
    assert line.index('export SOURCE_DIGEST_') < line.index('docker compose'), 'the stamp runs after the build'
    [(files, rest)] = _compose_calls(line)
    assert files == [BASE_FILE.name, FAKE_FILE.name] and rest == ['build', 'data_store', 'data_ingest'], (files, rest)
    assert f'DATA_DIR={TEST_DATA_DIR} docker compose' in line, line
    prod = _expanded_make_variable('PROD_COMPOSE', REPO_ROOT, _subprocess_env())
    assert prod not in line and 'docker-compose.web.yaml' not in line, line


def test_test_build_stamps_like_prod_build_and_dev_build():
    """Mirror: the same $(WITH_SOURCE_STAMP), so the same env names the base file's build args read."""
    stamps = {}
    for target, skipped in (
        (TEST_BUILD, ('proto',)),
        ('prod-build', ('proto', 'gen-proto-ts')),
        ('dev-build', ('proto', 'gen-proto-ts')),
    ):
        assert _make_recipe(target)[0].startswith('$(WITH_SOURCE_STAMP) '), (target, _make_recipe(target))
        stamps[target] = _exported_stamp(_dry_run(target, *skipped))
    assert stamps == dict.fromkeys(stamps, _stamp_names()), stamps


def test_test_build_hands_docker_the_real_digests(tmp_path: Path):
    """Run for real, docker stubbed: compose's process sees each service's own non-empty digest.

    Nothing but tools/source_digest.sh and the stub run (-o proto: no codegen). The values are the
    script's own output, so an unstamped build (empty stamp, which reads as 'cannot verify') is red.
    """
    env, log = _stub_docker(tmp_path)
    result = _run_make(REPO_ROOT, '-o', 'proto', TEST_BUILD, env=env)
    assert result.returncode == 0, f'{result.stdout}{result.stderr}'
    digests = []
    for service in ('store', 'ingest'):
        script = subprocess.run(
            ['./tools/source_digest.sh', 'data', service], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        )
        digests.append(script.stdout.strip())
    assert all(digests) and digests[0] != digests[1], digests
    calls = _docker_calls(log)
    assert len(calls) == 1, calls
    [(store, ingest, arguments)] = calls
    assert (store, ingest) == tuple(digests), (store, ingest, digests)
    [(files, rest)] = _compose_calls(f'docker {arguments}')
    assert files == [BASE_FILE.name, FAKE_FILE.name] and rest == ['build', 'data_store', 'data_ingest'], arguments


def test_test_build_prerequisites_are_the_venv_and_proto_and_not_the_web_codegen():
    """Like prod-build minus gen-proto-ts: test mode has no web service."""
    assert _prerequisites(TEST_BUILD) == ['$(VENV_MARKER)', 'proto'], _prerequisites(TEST_BUILD)


def test_test_build_is_phony():
    phony = [line for line in MAKEFILE.read_text(encoding='utf-8').splitlines() if line.startswith('.PHONY:')]
    assert any(TEST_BUILD in line.split(':', 1)[1].split() for line in phony)


# --- the guard and the comments the 22:50 UTC note required ----------------------------------------


def test_the_disposable_guard_offers_only_the_test_stack(tmp_path: Path):
    """22:50 UTC (A): the refusal says (make system-launch, then make migrate) and never make dev-launch.

    system-launch takes a dev stack down, and test-system or seed-dump against a dev stack would write
    into the owner's dev data -- so the only stack the guard may offer is the test one.
    """
    env, log = _stub_docker(tmp_path, **{SYSTEM_GUARD: None})
    for target in ('test-system', 'seed-dump', 'system-launch'):
        result = _run_make(tmp_path, target, env=env)
        assert result.returncode != 0 and 'REFUSED' in result.stderr, (target, result.stderr)
        assert GUARD_LINE in result.stderr, f'{target} refusal:\n{result.stderr}'
        assert 'dev-launch' not in result.stderr, f'{target} refusal offers a dev stack:\n{result.stderr}'
    assert _docker_calls(log) == []


def test_the_leave_fake_mode_comment_says_test_mode():
    """22:50 UTC (B): addendum 27 (1) merged TEST and SYSTEM, so the comment names one test mode."""
    text = MAKEFILE.read_text(encoding='utf-8')
    assert '# test mode: docker-compose.fake.yaml among its com.docker.compose.project.config_files' in text
    assert 'TEST or SYSTEM mode' not in text


def test_the_migrate_check_wording_names_each_stacks_own_build():
    """tj-iee6sn.24, the architect's 09:30 note (A), pinned loosely: a test stack's fix is make test-build.

    The comment and the ## help line both say so, and no longer call a system-launch image 'the ordinary case'.
    """
    text = MAKEFILE.read_text(encoding='utf-8')
    comment = ' '.join(
        line.lstrip('# ').strip() for line in text.split('.PHONY: migrate-check', 1)[0].splitlines()[-30:]
    )
    assert 'on a test stack' in comment and ':test' in comment and 'make test-build' in comment, comment
    [help_line] = [line for line in text.splitlines() if line.startswith('migrate-check:')]
    for phrase in ('make dev-build (dev)', 'make test-build (test)', 'make prod-build (prod)'):
        assert phrase in help_line, (phrase, help_line)
    assert 'system-launch image' not in help_line, help_line


# --- CI: System Testing runs the test pair from :test; the deploy build stays on :latest ------------


def _commands_named(step: dict, name: str) -> list[list[str]]:
    return [line.split() for line in _step_lines(step) if line.split()[:2] == ['docker', name]]


def test_ci_tags_exactly_the_pulled_images_as_test():
    """Addendum 27 (2), the smallest edit: `docker tag` of the images the job pulled, to the :test tags.

    The source of each :test tag is the image Image Build pushed from this run's commit (the build
    job's output), the same one the job pulls -- never a registry :latest that could be stale.
    """
    step = _system_step(PULL_STEP)
    outputs = {
        'data_store': '${{ needs.build.outputs.IMAGE_STORE }}',
        'data_ingest': '${{ needs.build.outputs.IMAGE_INGEST }}',
    }
    pulls = [' '.join(words[2:]) for words in _commands_named(step, 'pull')]
    assert sorted(pulls) == sorted(outputs.values()), pulls
    tags = [(' '.join(words[2:-1]), words[-1]) for words in _commands_named(step, 'tag')]
    to_test = sorted((source, target) for source, target in tags if target.endswith(':test'))
    assert to_test == sorted((outputs[name], TEST_IMAGES[name]) for name in TEST_IMAGES), tags
    assert BUILD_JOB in (_system_job().get('needs') or []), _system_job().get('needs')


def _build_job() -> dict:
    jobs = (_load_yaml(TESTING_WORKFLOW) or {}).get('jobs') or {}
    assert BUILD_JOB in jobs, sorted(jobs)
    return jobs[BUILD_JOB]


def test_ci_deploy_build_and_push_stay_on_latest():
    """The Image Build job is untouched: it builds the base file (:latest) and pushes that; no :test."""
    text = '\n'.join(step.get('run') or '' for step in _build_job().get('steps') or [])
    assert ':test' not in text, 'the Image Build job names a :test tag'
    assert 'docker compose -f docker-compose.yaml build' in text
    for image in ('trader_joe_data_store', 'trader_joe_data_ingest'):
        assert re.search(rf'^\s*docker tag {image} \$IMAGE_', text, re.MULTILINE), f'{image} is not pushed from :latest'


def test_ci_migrates_through_the_test_pair():
    """Both one-offs in Migrate Database -- the upgrade and the revisions() helper -- load the overlay."""
    calls = [(files, rest) for line in _step_lines(_system_step(MIGRATE_STEP)) for files, rest in _compose_calls(line)]
    assert len(calls) == 2, calls
    for files, rest in calls:
        assert files == [BASE_FILE.name, FAKE_FILE.name], calls
        assert rest[:1] == ['run'] and 'data_store' in rest and '--no-deps' in rest, calls
    assert any('upgrade' in rest and 'head' in rest for _, rest in calls), calls
    assert any('"$1"' in rest for _, rest in calls), calls


def test_ci_starts_the_stack_by_make_system_launch_alone():
    """Start System is unchanged: make system-launch, so the stack is the overlay's :test pair."""
    makes = [line for line in _step_lines(_system_step(START_STEP)) if line.split()[:1] == ['make']]
    assert makes == ['make system-launch'], makes
