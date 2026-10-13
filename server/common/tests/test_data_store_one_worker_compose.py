"""build_infra pin: data_store runs ONE uvicorn worker in every launch set (validator, gating tj-vcgqw8).

WHY ONE. The startup sweep of RUNNING ingest runs (data/store/app/ingest/run_recording.py) and the in-process events
hub (data/store/app/events.py) are correct only in a single process: with two workers one worker's sweep fails the
other's live run, and an event reaches only the clients of the worker that published it.

WHAT HOLDS THE NUMBER. entrypoint.sh starts uvicorn with --workers "$SERVICE_WORKERS". data_store's env_file is the
OPERATOR'S copy of server/data/store/.env, which may say anything, so the base compose file pins SERVICE_WORKERS=1
under environment:, which compose ranks above env_file: (and above the operator's shell, since the value is a
literal, not an interpolation). Pinned here, over the compose files as parsed (no daemon in the agent container,
so `docker compose config` is not run; the merge is compose_model's):

  * the base entry is the literal 1 and interpolates to 1 even with SERVICE_WORKERS=2 in the environment;
  * every Makefile launch set that starts data_store resolves it to 1 after its overlays merge;
  * no other compose file sets it, and none replaces data_store's command or entrypoint (which would bypass
    entrypoint.sh and with it the variable);
  * entrypoint.sh still hands the variable to uvicorn, so the pin reaches the process.
"""

from typing import Final

import pytest

from common.tests.compose_model import BASE_FILE, DEVCONTAINER_COMPOSE, interpolate, load, merge
from common.tests.roots import REPO_ROOT
from common.tests.test_grpc_bind_network import _set_files


pytestmark = pytest.mark.build_infra

SERVICE: Final = 'data_store'
VARIABLE: Final = 'SERVICE_WORKERS'
ENTRYPOINT: Final = REPO_ROOT / 'entrypoint.sh'
COMPOSE_FILES: Final = [*sorted(REPO_ROOT.glob('docker-compose*.yaml')), DEVCONTAINER_COMPOSE]
# Every Makefile set whose files define data_store (AGENT_COMPOSE and AGENT_MCP_COMPOSE start other things).
LAUNCH_SETS: Final = (
    'PROD_COMPOSE',
    'DEV_COMPOSE',
    'TOOLS_COMPOSE',
    'AGENT_STACK_COMPOSE',
    'TEST_CLIENT_COMPOSE',
    'SYSTEM_COMPOSE',
    'SEED_DUMP_COMPOSE',
)


def _environment(service: dict) -> dict[str, str | None]:
    node = service.get('environment') or {}
    if isinstance(node, dict):
        return {str(key): None if value is None else str(value) for key, value in node.items()}
    entries: dict[str, str | None] = {}
    for entry in node:
        key, separator, value = str(entry).partition('=')
        entries[key] = value if separator else None
    return entries


def test_the_base_file_pins_one_worker_as_a_literal_that_no_environment_changes():
    service = load(BASE_FILE)['services'][SERVICE]
    value = _environment(service).get(VARIABLE)
    assert value == '1', f'{VARIABLE} in data_store environment is {value!r}'
    assert interpolate(value, {VARIABLE: '2'}) == '1', 'the pin must not read the operator environment'
    assert service.get('env_file'), 'precondition: data_store reads an operator env file the pin must outrank'


@pytest.mark.parametrize('variable', LAUNCH_SETS)
def test_every_launch_set_resolves_data_store_to_one_worker(variable: str):
    files = _set_files(variable)
    merged = merge([load(path) for path in files])
    service = merged['services'][SERVICE]
    assert _environment(service).get(VARIABLE) == '1', (variable, [path.name for path in files])
    assert 'command' not in service and 'entrypoint' not in service, (variable, service.get('command'))


@pytest.mark.parametrize('path', [path for path in COMPOSE_FILES if path != BASE_FILE], ids=lambda path: path.name)
def test_no_other_compose_file_sets_the_worker_count_or_replaces_the_command(path):
    service = (load(path).get('services') or {}).get(SERVICE)
    if service is None:
        return
    assert VARIABLE not in _environment(service), f'{path.name} sets {VARIABLE} for {SERVICE}'
    assert 'command' not in service and 'entrypoint' not in service, f'{path.name} bypasses entrypoint.sh'


def test_entrypoint_hands_the_variable_to_uvicorn():
    assert '--workers "$SERVICE_WORKERS"' in ENTRYPOINT.read_text(encoding='utf-8')
