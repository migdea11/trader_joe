"""build_infra pin for the compose wiring of GET /ui/v1/config's two settings (tj-grna9p.101).

server/data/store/app/ui_config.py reads DEPLOYMENT_LABEL and SERVER_VERSION, and falls back on its own when
either is unset or empty (test_ui_config_route.py pins the fallbacks). docker-compose.yaml hands both to
data_store as BARE keys: compose passes the shell's or the project .env's value and omits the variable when
neither sets it. Pinned here: the base file spells them bare, so a removal or a ':-' default that would mask the
fallback fails; and no overlay of any launch set gives either one a value, but for the two labels: the dev
override's 'dev' and (tj-grna9p.148) the test-mode overlay's 'test'.
"""

from typing import Final

import pytest

from common.tests.compose_model import (
    BASE_FILE,
    FAKE_FILE,
    client_model,
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


@pytest.mark.parametrize('variable', ['PROD_COMPOSE'])
def test_no_prod_launch_set_loads_the_dev_override(variable: str):
    files = _set_files(variable)
    assert OVERRIDE_FILE not in files, (variable, [path.name for path in files])
    environment = merge([load(path) for path in files])['services'][SERVICE]['environment']
    assert environment.get(LABEL) == '', environment.get(LABEL)
