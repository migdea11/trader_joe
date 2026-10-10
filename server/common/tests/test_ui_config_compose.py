"""build_infra pin for the compose wiring of GET /ui/v1/config's two settings (tj-grna9p.101).

server/data/store/app/ui_config.py reads DEPLOYMENT_LABEL and SERVER_VERSION, and falls back on its own when
either is unset or empty (test_ui_config_route.py pins the fallbacks). docker-compose.yaml hands both to
data_store as BARE keys: compose passes the shell's or the project .env's value and omits the variable when
neither sets it. Pinned here: the base file spells them bare, so a removal or a ':-' default that would mask the
fallback fails; and no overlay of any launch set gives either one a value.
"""

from typing import Final

import pytest

from common.tests.compose_model import BASE_FILE, agent_stack_model, client_model, load, prod_model, system_model


pytestmark = pytest.mark.build_infra

SERVICE: Final = 'data_store'
BARE_KEYS: Final = ('DEPLOYMENT_LABEL', 'SERVER_VERSION')


def test_data_store_receives_both_settings_as_bare_keys():
    environment = [str(entry) for entry in load(BASE_FILE)['services'][SERVICE]['environment']]
    for key in BARE_KEYS:
        assert key in environment, f'{key} must be a bare entry in data_store environment'
        assert not [entry for entry in environment if entry.startswith(f'{key}=')], f'{key} must carry no value'


@pytest.mark.parametrize('model', [prod_model, client_model, agent_stack_model, system_model])
def test_no_launch_set_gives_either_setting_a_value(model):
    environment = model()['services'][SERVICE]['environment']
    for key in BARE_KEYS:
        assert environment.get(key) == '', (key, environment.get(key))
