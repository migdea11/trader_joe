"""The revisions ship in the data_store image; restore and rehearse targets; the CI migration gate (bead tj-7zjov1.10).

Design checked against: bead tj-7zjov1.10 (P04) and the architect's notes on it -- COPY alembic.ini and
migrations into the image and drop the bind mounts; Makefile restore and rehearse targets calling P03's
server/data/store/restore_dump.sh and rehearse_migration.sh, with no standalone backup target and
pg_dump never spelled in the Makefile (option (a): make migrate takes the dump itself); a CI job that
applies every revision from empty and upgrades the previous release's seed. Folds tj-y3sj8x.

Expected values are literals: a test that read them from the file under test would agree with any change.

THE PARTS THIS CANNOT REACH, said here so they are not read as covered: no docker runs in this suite, so
the image build is not exercised. In particular, the selection of the revision stage -- global ARGs
before the first FROM, `FROM migrations_${SERVICE_PATH}_${SERVICE_NAME} AS migrations_selected`, and
`COPY --from=migrations_selected` (tj-7zjov1.95: Docker refuses a build arg in --from) -- and whether
copying the EMPTY scratch stage migrations_data_ingest is a no-op are pinned as Dockerfile TEXT below,
never as built behaviour; and the Migration Gate job is pinned as workflow text, not run.

Neighbours, not repeated here: the data_store and data_ingest import guards
(server/data/*/tests/test_no_production_test_imports.py) read the revision stage as part of what each
image ships; test_layout_invariants pins that no compose file mounts over the revision paths; and
tools/agent_mcp/tests/test_source_digest_context.py pins that both service digests cover the revisions.

MARKER: build_infra -- the Dockerfile, compose file, Makefile and workflow are what is driven.
"""

import configparser
import re
from pathlib import Path, PurePosixPath

import pytest

from common.tests import compose_model
from common.tests.test_ci_invariants import (
    MAKEFILE,
    REPO_ROOT,
    SYSTEM_GUARD,
    TESTING_WORKFLOW,
    _load_yaml,
    _make_recipe,
    _run_lines,
    _run_make,
    _subprocess_env,
)


pytestmark = pytest.mark.build_infra

DOCKERFILE = REPO_ROOT / 'Dockerfile'
STORE_STAGE = 'migrations_data_store'
INGEST_STAGE = 'migrations_data_ingest'
SOURCE_STAGE = 'service_build_image'
SELECTED_STAGE = 'migrations_selected'
SELECTOR_LINE = f'FROM migrations_${{SERVICE_PATH}}_${{SERVICE_NAME}} AS {SELECTED_STAGE}'
STAGE_COPY_LINE = f'COPY --from={SELECTED_STAGE} / /code/'
GLOBAL_ARGS = ['ARG SERVICE_PATH=none', 'ARG SERVICE_NAME=none']
APP_COPY_LINE = 'COPY ./server/${SERVICE_PATH}/${SERVICE_NAME}/app /code/${SERVICE_PATH}/${SERVICE_NAME}/app'
STORE_STAGE_BODY = [
    'COPY ./server/data/store/alembic.ini /alembic.ini',
    'COPY ./server/data/store/migrations /migrations',
]
REVISION_TARGETS = ('/code/alembic.ini', '/code/migrations')
REVISION_SOURCES = ('server/data/store/alembic.ini', 'server/data/store/migrations')


# --- the Dockerfile ----------------------------------------------------------------------------------


def _instructions() -> list[str]:
    """The Dockerfile's logical instructions: continuations joined, comments and blank lines dropped."""
    text = DOCKERFILE.read_text(encoding='utf-8').replace('\\\n', ' ')
    return [' '.join(line.split()) for line in text.splitlines() if line.strip() and not line.strip().startswith('#')]


def _stage_index(instructions: list[str], stage: str) -> int:
    headers = [
        index for index, line in enumerate(instructions) if line.startswith('FROM ') and line.split()[-1] == stage
    ]
    assert len(headers) == 1, f'expected one stage named {stage}, found {len(headers)}'
    return headers[0]


def _stage(stage: str) -> tuple[str, list[str]]:
    """(the stage's FROM line, its instructions up to the next FROM)."""
    instructions = _instructions()
    start = _stage_index(instructions, stage)
    body = []
    for line in instructions[start + 1 :]:
        if line.startswith('FROM '):
            break
        body.append(line)
    return instructions[start], body


def test_the_store_revision_stage_is_scratch_holding_exactly_alembic_ini_and_migrations():
    header, body = _stage(STORE_STAGE)
    assert header == f'FROM scratch AS {STORE_STAGE}', header
    assert body == STORE_STAGE_BODY, body


def test_the_ingest_revision_stage_is_empty_scratch():
    """data_ingest has no revisions: its stage exists only so the by-name COPY resolves, and copies nothing."""
    header, body = _stage(INGEST_STAGE)
    assert header == f'FROM scratch AS {INGEST_STAGE}', header
    assert body == [], body


def test_the_source_stage_copies_the_selected_revision_stage_after_the_app():
    header, body = _stage(SOURCE_STAGE)
    assert header == f'FROM base_build_image AS {SOURCE_STAGE}', header
    assert body.count(STAGE_COPY_LINE) == 1, [line for line in body if '--from' in line]
    assert [line for line in body if line.startswith('COPY --from=')] == [STAGE_COPY_LINE], body
    # The stage's own args still feed its uv sync and the app COPY, so they come before the app COPY.
    assert body.index('ARG SERVICE_PATH=none') < body.index(APP_COPY_LINE), body
    assert body.index('ARG SERVICE_NAME=none') < body.index(APP_COPY_LINE), body
    assert body.index(APP_COPY_LINE) < body.index(STAGE_COPY_LINE), body


def test_no_copy_from_in_the_dockerfile_expands_a_build_arg():
    """Docker stops the build on a build arg in --from ("variable expansion is not supported for --from")."""
    instructions = _instructions()
    from_operands = [word for line in instructions for word in line.split() if word.startswith('--from=')]
    assert STAGE_COPY_LINE in instructions and from_operands, 'non-vacuity: no COPY --from left to read'
    assert not [operand for operand in from_operands if '$' in operand], from_operands


def test_the_selector_args_are_global_declared_before_the_first_from():
    """A FROM may expand only an ARG declared before the first FROM; one inside a stage would leave it empty."""
    instructions = _instructions()
    first_from = next(index for index, line in enumerate(instructions) if line.startswith('FROM '))
    assert instructions[:first_from] == GLOBAL_ARGS, instructions[:first_from]


def test_the_selector_stage_is_a_bare_from_between_the_revision_stages_and_the_source_stage():
    """The selector follows both stages it may name and precedes the stage whose COPY --from names it.

    A FROM names only an earlier stage. It adds nothing of its own, so it ships exactly the stage it picks.
    """
    header, body = _stage(SELECTED_STAGE)
    assert header == SELECTOR_LINE, header
    assert body == [], body
    instructions = _instructions()
    selector = _stage_index(instructions, SELECTED_STAGE)
    assert _stage_index(instructions, STORE_STAGE) < selector
    assert _stage_index(instructions, INGEST_STAGE) < selector
    assert selector < _stage_index(instructions, SOURCE_STAGE)


def test_both_revision_stages_precede_the_source_stage():
    """A stage must be defined before the COPY --from that names it, or it is read as an image."""
    instructions = _instructions()
    source = _stage_index(instructions, SOURCE_STAGE)
    assert _stage_index(instructions, STORE_STAGE) < source
    assert _stage_index(instructions, INGEST_STAGE) < source


@pytest.mark.parametrize(('service', 'stage'), [('data_store', STORE_STAGE), ('data_ingest', INGEST_STAGE)])
def test_each_services_build_args_name_its_revision_stage(service: str, stage: str):
    """The selector's FROM, expanded with compose's build args, names the service's stage: data_store -> its own."""
    args = (compose_model.load(compose_model.BASE_FILE)['services'][service]['build'] or {}).get('args') or {}
    parent = _stage(SELECTED_STAGE)[0].split()[1]
    resolved = parent.replace('${SERVICE_PATH}', args['SERVICE_PATH']).replace('${SERVICE_NAME}', args['SERVICE_NAME'])
    assert resolved == stage, (service, args, parent)


RUN_MIGRATIONS = REPO_ROOT / 'server' / 'data' / 'store' / 'run_migrations.sh'
ALEMBIC_INI = REPO_ROOT / REVISION_SOURCES[0]
DEPLOY_STAGES = ('prod_image', 'dev_image')


def _copies(body: list[str]) -> list[tuple[str | None, str, str]]:
    """(the --from stage or None, source, destination) of every plain two-operand COPY in a stage body."""
    copies = []
    for line in body:
        words = line.split()
        if words[0] != 'COPY':
            continue
        source_stage = None
        if words[1].startswith('--from='):
            source_stage = words[1].removeprefix('--from=')
            words = words[1:]
        assert len(words) == 3, f'a COPY this test cannot read as one source and one destination: {line}'
        copies.append((source_stage, words[1], words[2]))
    return copies


def _rebase(path: PurePosixPath, source: str, destination: str) -> PurePosixPath:
    """Where `path`, inside a COPY's source tree, lands under its destination."""
    return PurePosixPath(destination) / path.relative_to(PurePosixPath(source))


def _image_path_of(checkout_source: str) -> PurePosixPath:
    """Where the data_store image keeps a file the store revision stage copies, read hop by hop from the Dockerfile.

    The revision stage puts it at its COPY destination; service_build_image's one COPY --from of the revision
    stage moves that tree under its destination; each deploy stage's COPY of service_build_image's /code carries
    it into the final image. No path in this function is a literal: every hop is the Dockerfile's own.
    """
    _, store_body = _stage(STORE_STAGE)
    landed = [destination for _, source, destination in _copies(store_body) if source == f'./{checkout_source}']
    assert len(landed) == 1, (checkout_source, store_body)
    path = PurePosixPath(landed[0])

    _, source_body = _stage(SOURCE_STAGE)
    from_revision_stage = [copy for copy in _copies(source_body) if (copy[0] or '').startswith('migrations_')]
    assert len(from_revision_stage) == 1, source_body
    _, source, destination = from_revision_stage[0]
    path = _rebase(path, source, destination)

    final = set()
    for stage in DEPLOY_STAGES:
        _, body = _stage(stage)
        carried = [
            _rebase(path, source, destination)
            for from_stage, source, destination in _copies(body)
            if from_stage is not None and from_stage.startswith(SOURCE_STAGE) and path.is_relative_to(source)
        ]
        assert len(carried) == 1, (stage, body)
        final.add(carried[0])
    assert len(final) == 1, f'the deploy stages put {checkout_source} in different places: {sorted(map(str, final))}'
    return final.pop()


def _script_image_migration_dir() -> PurePosixPath:
    values = re.findall(r'^IMAGE_MIGRATION_DIR=(\S+)$', RUN_MIGRATIONS.read_text(encoding='utf-8'), re.MULTILINE)
    assert len(values) == 1, f'expected one top-level IMAGE_MIGRATION_DIR= in {RUN_MIGRATIONS.name}, found {values}'
    return PurePosixPath(values[0])


def test_run_migrations_looks_for_the_revisions_where_the_dockerfile_puts_them():
    """run_migrations.sh's IMAGE_MIGRATION_DIR is the versions directory of the migrations the image carries.

    Carried from the gate of tj-7zjov1.45 onto tj-7zjov1.11 (architect, 11:25 UTC 2026-10-24). The script's
    guard looks for revision files at IMAGE_MIGRATION_DIR inside the data_store image before it migrates, and
    refuses when there are none. The tests above, and the data_store import guard
    (test_the_revision_roots_are_the_revision_stage_copies), pin the Dockerfile side as literals; the script's
    value is pinned as a literal too (test_roots, the compose doubles of the store's script tests). Nothing
    compared the two sides, so moving the stage together with its test literals left every test green, and the
    first sign would be the runtime guard refusing a real migrate. Here the image path is DERIVED from the
    Dockerfile (the revision stage's destination, rebased by service_build_image's COPY --from of that stage
    and by the deploy stages' COPY of /code) and compared with the script's value. The one literal is
    'versions', alembic's own directory name under script_location.
    """
    migrations = _image_path_of(REVISION_SOURCES[1])
    script_dir = _script_image_migration_dir()
    assert script_dir.parent == migrations, (
        f'run_migrations.sh looks for revisions in {script_dir}, but the Dockerfile puts the migrations at '
        f'{migrations} in the data_store image'
    )
    assert script_dir.name == 'versions', script_dir
    # Non-vacuous: the checkout's tree has the same shape, so the directory the script names is a real one.
    assert list((REPO_ROOT / REVISION_SOURCES[1] / script_dir.name).glob('*.py')), script_dir.name


def test_alembic_ini_lands_in_the_working_directory_and_resolves_to_the_shipped_migrations():
    """alembic.ini lands in the deploy image's WORKDIR, and its script_location resolves to the shipped migrations.

    alembic runs in the image with no -c, so it reads ./alembic.ini from WORKDIR. run_migrations.sh runs `/code/.venv/bin/alembic <args>` in a one-off data_store container, so the ini is
    found only if it lands in the deploy image's WORKDIR, and a relative script_location is resolved against
    that same directory. The data_store import guard (test_alembic_loads_scripts_only_from_a_scanned_revision_root)
    resolves script_location against a literal /code; here WORKDIR and both paths are read from the Dockerfile.
    """
    ini = _image_path_of(REVISION_SOURCES[0])
    migrations = _image_path_of(REVISION_SOURCES[1])
    workdirs = {line.split()[1] for line in _stage('base_deploy_image')[1] if line.startswith('WORKDIR ')}
    assert workdirs == {str(ini.parent)}, (workdirs, ini)

    config = configparser.ConfigParser()
    config.read(ALEMBIC_INI, encoding='utf-8')
    location = PurePosixPath(config['alembic']['script_location'])
    assert not location.is_absolute(), location
    assert ini.parent / location == migrations, (ini, location, migrations)
    # The script's own invocation is what makes "no -c, so ./alembic.ini" true.
    invocations = [
        line
        for line in RUN_MIGRATIONS.read_text(encoding='utf-8').splitlines()
        if '/.venv/bin/alembic' in line and not line.lstrip().startswith('#')
    ]
    assert invocations, 'run_migrations.sh no longer invokes alembic in the container'
    assert not [line for line in invocations if re.search(r'\s-c\s', line) or '--config' in line], invocations


def test_the_revision_sources_exist_and_hold_revisions():
    """Non-vacuous: what the store stage copies is there, a file and a tree with revisions in it."""
    assert (REPO_ROOT / REVISION_SOURCES[0]).is_file()
    versions = REPO_ROOT / REVISION_SOURCES[1] / 'versions'
    assert (REPO_ROOT / REVISION_SOURCES[1] / 'env.py').is_file() and list(versions.glob('*.py')), versions


# --- compose -----------------------------------------------------------------------------------------


def test_the_base_compose_file_gives_data_store_no_volumes():
    """The revision bind mounts are gone, and with them the only volumes data_store had."""
    service = compose_model.load(compose_model.BASE_FILE)['services']['data_store']
    assert 'volumes' not in service, service.get('volumes')


@pytest.mark.parametrize('name', sorted(path.name for path in REPO_ROOT.glob('docker-compose*.yaml')))
def test_no_compose_file_mounts_the_store_revisions_from_the_checkout_into_data_store(name: str):
    """Source side, every repo-root file: data_store never binds the checkout's alembic.ini or migrations."""
    service = (compose_model.load(REPO_ROOT / name).get('services') or {}).get('data_store') or {}
    sources = [
        compose_model.volume(entry)['source'].removeprefix('./').rstrip('/') for entry in service.get('volumes') or []
    ]
    assert not [source for source in sources if source.startswith(REVISION_SOURCES)], (name, sources)


# --- the Makefile ------------------------------------------------------------------------------------

RESTORE_TARGETS = {'migrate-restore': 'restore_dump.sh', 'migrate-rehearse': 'rehearse_migration.sh'}
SCRIPT_DIR = Path('server/data/store')
# Every name a dump or restore command goes by. make migrate takes the dump through run_migrations.sh;
# a second spelling here would drift from it (the architect's 2026-10-24 note on this bead).
DUMP_COMMANDS = re.compile(r'\bpg_(?:dump|dumpall|restore)\b')
# The stub each target must reach: it records its argv and the attestation it saw, and nothing else.
SCRIPT_STUB = """#!/bin/sh
{ printf '%s\\037' "$(basename "$0")" "${SYSTEM_TEST_DISPOSABLE_DB-<unset>}"; for word in "$@"; do printf '%s\\037' "$word"; done; printf '\\036'; } >> "$STUB_LOG"
exit 0
"""


def _stubbed(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """A working directory whose server/data/store holds stub scripts, and the env make runs under."""
    log = tmp_path / 'scripts.log'
    for script in ('restore_dump.sh', 'rehearse_migration.sh', 'run_migrations.sh'):
        path = tmp_path / SCRIPT_DIR / script
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SCRIPT_STUB, encoding='utf-8')
        path.chmod(0o755)
    env = _subprocess_env(**{SYSTEM_GUARD: None, 'DUMP': None})
    env['STUB_LOG'] = str(log)
    return env, log


def _script_calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [record.split('\037')[:-1] for record in log.read_text(encoding='utf-8').split('\036') if record]


@pytest.mark.parametrize(('target', 'script'), sorted(RESTORE_TARGETS.items()))
def test_the_target_calls_exactly_its_script_with_the_dump_and_the_attestation(tmp_path: Path, target, script):
    """One call, of the one P03 script, with DUMP as its only argument and the attestation set.

    SYSTEM_TEST_DISPOSABLE_DB=1 is set by the recipe itself: the caller's environment carries none here.
    """
    env, log = _stubbed(tmp_path)
    dump = '/backups/2026-10-24 store.dump'
    result = _run_make(tmp_path, target, f'DUMP={dump}', env=env)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert _script_calls(log) == [[script, '1', dump]], _script_calls(log)


@pytest.mark.parametrize('target', sorted(RESTORE_TARGETS))
@pytest.mark.parametrize('dump', [None, ''], ids=['unset', 'empty'])
def test_the_target_refuses_without_a_dump_and_runs_nothing(tmp_path: Path, target: str, dump: str | None):
    env, log = _stubbed(tmp_path)
    arguments = [] if dump is None else [f'DUMP={dump}']
    result = _run_make(tmp_path, target, *arguments, env=env)
    assert result.returncode != 0, result.stdout
    assert f'make {target}: set DUMP=<dump file>' in result.stderr, result.stderr
    assert _script_calls(log) == [], _script_calls(log)


@pytest.mark.parametrize(('target', 'script'), sorted(RESTORE_TARGETS.items()))
def test_the_recipe_is_the_guard_and_one_script_line(target: str, script: str):
    """Thin spellings: the scripts hold the logic and the refusals, so the recipe holds nothing else."""
    recipe = _make_recipe(target)
    assert len(recipe) == 2, recipe
    assert recipe[1] == f'{SYSTEM_GUARD}=1 ./{SCRIPT_DIR / script} "$(DUMP)"', recipe
    phony = re.findall(r'^\.PHONY:\s*(.+)$', MAKEFILE.read_text(encoding='utf-8'), re.MULTILINE)
    assert target in ' '.join(phony).split(), f'{target} is not .PHONY'


def test_the_rehearse_help_says_the_revisions_are_the_running_test_images():
    """The revisions rehearse applies ship in the :test image; they match the checkout only after make test-build."""
    text = MAKEFILE.read_text(encoding='utf-8')
    match = re.search(r'^migrate-rehearse:\s*##\s*(.+)$', text, re.MULTILINE)
    assert match is not None, 'migrate-rehearse carries no help line'
    help_text = match.group(1)
    assert "the running test image's migrations" in help_text, help_text
    assert 'make test-build' in help_text, help_text
    assert "this checkout's migrations" not in help_text, help_text


def test_the_makefile_never_spells_a_dump_or_restore_command():
    """Anywhere in the file, comments included: the one dump is run_migrations.sh's."""
    lines = MAKEFILE.read_text(encoding='utf-8').splitlines()
    spelled = [f'{number}: {line}' for number, line in enumerate(lines, 1) if DUMP_COMMANDS.search(line)]
    assert not spelled, spelled


def test_there_is_no_standalone_backup_target():
    """Option (a): no backup target and no placeholder for one; make migrate takes the dump."""
    targets = re.findall(r'^([A-Za-z0-9_.-]+)\s*:(?!=)', MAKEFILE.read_text(encoding='utf-8'), re.MULTILINE)
    assert 'migrate-restore' in targets, 'non-vacuity: the target scan found nothing'
    dump_targets = [
        target for target in targets if 'backup' in target or (target.endswith('-dump') and target != 'seed-dump')
    ]
    assert not dump_targets, dump_targets


# --- the CI migration gate ---------------------------------------------------------------------------

MIGRATION_JOB_ID = 'migration'
MIGRATION_JOB_NAME = 'Migration Gate'
START_STEP = 'Start System'
FROM_EMPTY_STEP = 'Apply Every Revision From Empty'
SEED_STEP = 'Upgrade The Previous Release Seed'
SEED_TEST = 'tests/system/test_migration_with_data.py'


def _jobs() -> dict:
    return (_load_yaml(TESTING_WORKFLOW) or {}).get('jobs') or {}


def _migration_steps() -> dict[str, dict]:
    job = _jobs().get(MIGRATION_JOB_ID) or {}
    assert job.get('name') == MIGRATION_JOB_NAME, job.get('name')
    steps = job.get('steps') or []
    named = {step.get('name'): step for step in steps}
    assert len(named) == len(steps), 'two steps of the migration job share a name'
    return named


def test_the_migration_job_runs_on_the_built_images_in_order():
    job = _jobs()[MIGRATION_JOB_ID]
    assert 'build' in (job.get('needs') or []), job.get('needs')
    names = [step.get('name') for step in job.get('steps') or []]
    assert names.index(START_STEP) < names.index(FROM_EMPTY_STEP) < names.index(SEED_STEP), names
    assert _run_lines(_migration_steps()[START_STEP]['run']) == ['set -euo pipefail', 'make system-launch']


def test_the_from_empty_step_migrates_twice_around_the_head_and_single_head_checks():
    """Every revision from empty, then (head) and exactly one head, then make migrate again: a no-op at head."""
    lines = _run_lines(_migration_steps()[FROM_EMPTY_STEP]['run'])
    migrates = [index for index, line in enumerate(lines) if line == 'make migrate']
    assert len(migrates) == 2, lines
    head_check = next(index for index, line in enumerate(lines) if line.startswith('*"(head)"*)'))
    heads_check = next(index for index, line in enumerate(lines) if "grep -cE '^[0-9a-f]{12}')\" -ne 1" in line)
    current = next(index for index, line in enumerate(lines) if 'run_migrations.sh current)' in line)
    heads = next(index for index, line in enumerate(lines) if 'run_migrations.sh heads)' in line)
    assert migrates[0] < current < head_check < migrates[1], lines
    assert migrates[0] < heads < heads_check < migrates[1], lines
    # The checks fail the step: each is followed by an exit 1 before the second migrate.
    assert sum('exit 1' in line for line in lines[head_check : migrates[1]]) >= 2, lines
    assert lines[0] == 'set -euo pipefail', lines


def test_the_seed_step_runs_the_seed_upgrade_test():
    lines = _run_lines(_migration_steps()[SEED_STEP]['run'])
    assert f'make test-system SYSTEM_PATHS={SEED_TEST}' in lines, lines
    assert (REPO_ROOT / SEED_TEST).is_file(), SEED_TEST


def test_no_step_of_the_workflow_stages_or_restores_the_revisions():
    """The pipeline-configs artifact carries no alembic.ini or migrations, and System Testing restores none."""
    staged = [
        f'{job.get("name")} / {step.get("name")}: {line}'
        for job in _jobs().values()
        for step in (job or {}).get('steps') or []
        for line in _run_lines(step.get('run') or '')
        if 'alembic.ini' in line or re.search(r'(?:artifact|store)/(?:data/store/)?migrations\b', line)
    ]
    assert not staged, staged
    # Non-vacuous: the step that builds the artifact is still there and still copies configs.
    stage_lines = [
        line
        for job in _jobs().values()
        for step in (job or {}).get('steps') or []
        if step.get('name') == 'Stage Pipeline Configs'
        for line in _run_lines(step.get('run') or '')
    ]
    assert any(line.startswith('cp ') and 'artifact/' in line for line in stage_lines), stage_lines


def test_the_migration_job_takes_its_dumps_inside_the_workspace():
    """The dump make migrate takes before it upgrades lands in the runner's throwaway workspace."""
    env = _jobs()[MIGRATION_JOB_ID].get('env') or {}
    assert env.get('MIGRATE_BACKUP_DIR', '').startswith('${{ github.workspace }}/'), env
