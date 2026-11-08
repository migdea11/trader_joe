"""build_infra pin for the compose wiring of GET /ui/v1/config's two settings (tj-grna9p.101).

server/data/store/app/ui_config.py reads DEPLOYMENT_LABEL and SERVER_VERSION, and falls back on its own when
either is unset or empty (test_ui_config_route.py pins the fallbacks). docker-compose.yaml hands both to
data_store as BARE keys: compose passes the shell's or the project .env's value and omits the variable when
neither sets it. Pinned here: the base file spells them bare, so a removal or a ':-' default that would mask the
fallback fails; and no overlay of any launch set gives either one a value, but for the two labels: the dev
override's 'dev' and (tj-grna9p.148) the test-mode overlay's 'test'. And (tj-7zjov1.11, m6) the production-label
pin: no file a production definition loads, compose file, env-file template or Dockerfile, gives the label a value.
"""

import re
from pathlib import Path
from typing import Final

import pytest

from common.tests.compose_model import (
    BASE_FILE,
    FAKE_FILE,
    client_model,
    interpolate,
    load,
    merge,
    prod_model,
    system_client_model,
    system_model,
)
from common.tests.roots import REPO_ROOT
from common.tests.test_grpc_bind_network import _set_files


pytestmark = pytest.mark.build_infra

SERVICE: Final = 'data_store'
BARE_KEYS: Final = ('DEPLOYMENT_LABEL', 'SERVER_VERSION')


def test_data_store_receives_both_settings_as_bare_keys():
    environment = [str(entry) for entry in load(BASE_FILE)['services'][SERVICE]['environment']]
    for key in BARE_KEYS:
        assert key in environment, f'{key} must be a bare entry in data_store environment'
        assert not [entry for entry in environment if entry.startswith(f'{key}=')], f'{key} must carry no value'


# tj-iee6sn.27: the retired agent stack's model (base, client, its overlay, fake) is re-pointed to
# system_client_model -- base, fake, client, as make seed-dump loads it -- the surviving set that
# carries both the fake overlay and the client file.
#
# tj-grna9p.148 re-pin (design (3)): the test-mode overlay now labels data_store 'test', so the two
# sets that load it resolve DEPLOYMENT_LABEL to 'test'. The guarantee kept: no set gives SERVER_VERSION
# a value, and the sets without the overlay (prod, the client) give neither one a value.
@pytest.mark.parametrize(
    ('model', 'label'),
    [(prod_model, ''), (client_model, ''), (system_client_model, 'test'), (system_model, 'test')],
    ids=['prod', 'client', 'system_client', 'system'],
)
def test_only_the_test_overlay_gives_the_label_a_value_and_nothing_gives_the_version_one(model, label):
    environment = model()['services'][SERVICE]['environment']
    assert environment.get('DEPLOYMENT_LABEL') == label, environment.get('DEPLOYMENT_LABEL')
    assert environment.get('SERVER_VERSION') == '', environment.get('SERVER_VERSION')


def test_the_test_label_lives_in_the_fake_overlay_on_data_store_alone():
    services = load(FAKE_FILE)['services']
    assert services[SERVICE]['environment'] == {LABEL: 'test'}
    for name, service in services.items():
        if name != SERVICE:
            entries = service.get('environment') or {}
            assert LABEL not in (
                entries if isinstance(entries, dict) else [str(e).partition('=')[0] for e in entries]
            ), name


# THE DEV LABEL (owner feedback tj-sww0b1 item 5). The UI renders nothing for an empty label and a
# highlighted tag for any other value but prod, so dev must SAY dev. The value lives in exactly one
# place, docker-compose.override.yaml, and only the dev launch set loads that file. Pinned three ways:
# the override sets it, neither base nor web overlay sets it, and no PROD set ever loads the override
# (read from the Makefile's own PROD_COMPOSE, not restated here, so a set that grows the file fails).
OVERRIDE_FILE: Final = REPO_ROOT / 'docker-compose.override.yaml'
WEB_FILE: Final = REPO_ROOT / 'docker-compose.web.yaml'
LABEL: Final = 'DEPLOYMENT_LABEL'


def test_the_dev_override_labels_data_store_dev():
    environment = merge([load(OVERRIDE_FILE)])['services'][SERVICE]['environment']
    assert environment.get(LABEL) == 'dev', environment.get(LABEL)


def test_the_dev_launch_set_resolves_the_label_to_dev():
    """The merged DEV_COMPOSE, in the Makefile's order: the override's value survives the web overlays."""
    environment = merge([load(path) for path in _set_files('DEV_COMPOSE')])['services'][SERVICE]['environment']
    assert environment.get(LABEL) == 'dev', environment.get(LABEL)


@pytest.mark.parametrize('path', [BASE_FILE, WEB_FILE], ids=lambda path: path.name)
def test_neither_the_base_nor_the_web_file_gives_the_label_a_value(path):
    for name, service in (load(path).get('services') or {}).items():
        entries = service.get('environment') or []
        values = entries.items() if isinstance(entries, dict) else [str(e).partition('=')[::2] for e in entries]
        for key, value in values:
            if key == LABEL:
                assert value in ('', None), f'{path.name} gives {name} {LABEL}={value!r}'


# THE PRODUCTION-LABEL PIN (m6 of the migration-safety gate; bead tj-7zjov1.11, PR5-PLAN N1 row P05). Production's
# DEPLOYMENT_LABEL is EMPTY, and run_migrations.sh refuses a migrate on the PRODUCTION definition when the
# data_store container would see anything else (tj-7zjov1.9, P03). That refusal is a run-time backstop; this is
# the committed-file half: NO FILE A PRODUCTION DEFINITION LOADS GIVES THE LABEL A VALUE. "A production
# definition" is read from the two places that define one, never restated: the Makefile's PROD_COMPOSE (how prod
# is launched) and run_migrations.sh's production lines (how prod is migrated). For every compose file either
# loads: no service's environment: sets the label, and every env_file: it names has a committed .default
# template that sets it to nothing. The Dockerfile is in it too: an ENV or ARG there would reach the container
# from under every compose file.
RUN_MIGRATIONS: Final = REPO_ROOT / 'server' / 'data' / 'store' / 'run_migrations.sh'
DOCKERFILE: Final = REPO_ROOT / 'Dockerfile'
_SCRIPT_DEFINITION = re.compile(r'^\s*COMPOSE=\(docker compose((?:\s+-f\s+[^\s)]+)+)\)\s*$')
_PRODUCTION_ECHO = 'using the PRODUCTION definition'


def _definition_files(line: str) -> tuple[Path, ...]:
    match = _SCRIPT_DEFINITION.match(line)
    assert match is not None, f'not a literal compose definition: {line!r}'
    return tuple(REPO_ROOT / name for name in re.findall(r'-f\s+(\S+)', match.group(1)))


def _script_production_definitions() -> list[tuple[Path, ...]]:
    """The files of run_migrations.sh's production definition: its top-level default and its production arm.

    The top-level COMPOSE=( line is the default (the script says so), and the production arm is the literal
    definition line nearest above the echo that announces the PRODUCTION definition.
    """
    lines = RUN_MIGRATIONS.read_text(encoding='utf-8').splitlines()
    top_level = [line for line in lines if line.startswith('COMPOSE=(')]
    assert len(top_level) == 1, top_level
    echoes = [index for index, line in enumerate(lines) if _PRODUCTION_ECHO in line]
    assert len(echoes) == 1, f'expected one echo naming the {_PRODUCTION_ECHO}, found {len(echoes)}'
    arm = next(line for line in reversed(lines[: echoes[0]]) if _SCRIPT_DEFINITION.match(line))
    return [_definition_files(top_level[0]), _definition_files(arm)]


def _production_files() -> list[Path]:
    files = set(_set_files('PROD_COMPOSE'))
    for definition in _script_production_definitions():
        files.update(definition)
    return sorted(files)


def _environment_entries(service: dict) -> list[tuple[str, str | None]]:
    entries = service.get('environment') or []
    if isinstance(entries, dict):
        return [(str(key), None if value is None else str(value)) for key, value in entries.items()]
    return [(key, value if separator else None) for key, separator, value in (str(e).partition('=') for e in entries)]


def _env_file_templates(document: dict) -> set[Path]:
    """The committed template of every env_file: entry, the entry's own default path plus '.default'."""
    templates = set()
    for service in (document.get('services') or {}).values():
        env_files = service.get('env_file') or []
        for entry in [env_files] if isinstance(env_files, str) else env_files:
            path = entry if isinstance(entry, str) else entry['path']
            resolved = interpolate(path, {}).removeprefix('./')
            assert resolved, f'env_file {path!r} resolves to nothing without the environment'
            templates.add(REPO_ROOT / f'{resolved}.default')
    return templates


def _env_file_value(path: Path, key: str) -> list[str]:
    return [
        line.partition('=')[2].strip()
        for line in path.read_text(encoding='utf-8').splitlines()
        if line.strip().removeprefix('export ').partition('=')[0].strip() == key
    ]


def test_the_production_definitions_are_read_and_name_the_base_file():
    """Non-vacuous: both readers find their definition, and both contain the base file."""
    assert BASE_FILE in _set_files('PROD_COMPOSE')
    definitions = _script_production_definitions()
    assert definitions and all(BASE_FILE in files for files in definitions), definitions
    assert OVERRIDE_FILE not in _production_files() and FAKE_FILE not in _production_files()


@pytest.mark.parametrize('path', _production_files(), ids=lambda path: path.name)
def test_no_file_a_production_definition_loads_gives_the_label_a_value(path: Path):
    """No service in the file sets DEPLOYMENT_LABEL to anything; a bare pass-through key is allowed."""
    document = load(path)
    assert document.get('services'), f'{path.name} defines no services'
    for name, service in document['services'].items():
        for key, value in _environment_entries(service or {}):
            if key == LABEL:
                assert value in (None, ''), f'{path.name} gives {name} {LABEL}={value!r}; production is empty'


def test_every_production_env_file_template_leaves_the_label_empty():
    """The env files prod loads are operator copies of committed templates; no template may set the label."""
    templates = set().union(*(_env_file_templates(load(path)) for path in _production_files()))
    assert REPO_ROOT / '.env.default' in templates, sorted(map(str, templates))
    for template in sorted(templates):
        assert template.is_file(), f'{template} is named by a production env_file and is not committed'
        values = _env_file_value(template, LABEL)
        assert all(value == '' for value in values), f'{template.name} sets {LABEL} to {values}'
    # The root template names the label, so the operator's copy shows it, empty.
    assert _env_file_value(REPO_ROOT / '.env.default', LABEL) == [''], 'the root template no longer names the label'


def test_the_image_never_sets_the_label():
    """An ENV or ARG in the Dockerfile would reach the prod container beneath every compose file."""
    lines = [line for line in DOCKERFILE.read_text(encoding='utf-8').splitlines() if not line.lstrip().startswith('#')]
    assert not [line for line in lines if LABEL in line], [line for line in lines if LABEL in line]


@pytest.mark.parametrize('variable', ['PROD_COMPOSE'])
def test_no_prod_launch_set_loads_the_dev_override(variable: str):
    files = _set_files(variable)
    assert OVERRIDE_FILE not in files, (variable, [path.name for path in files])
    environment = merge([load(path) for path in files])['services'][SERVICE]['environment']
    assert environment.get(LABEL) == '', environment.get(LABEL)
