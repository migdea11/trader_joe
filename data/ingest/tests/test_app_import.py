import subprocess
import sys
from pathlib import Path

import pytest


# The guard this file exists for (tj-8yix3i): twice now a module-scope dependency -- first the
# Alpaca client, then the Kafka producer -- made `import data.ingest.app.main` raise, and the
# suite stayed green because nothing anywhere imports an app main module. uvicorn imports it in
# a fresh interpreter at startup, so that is what this reproduces.
#
# To mirror this for another service, copy the file into that service's tests and change
# APP_MODULE, RPC_MODULE and EXPECTED_RPC_SERVERS -- nothing else. The server count is
# per-service: data_ingest registers its RPC server at module scope, data_store registers
# inside a function, so data_store's count is 0.
REPO_ROOT = Path(__file__).resolve().parents[3]
APP_MODULE = 'data.ingest.app.main'
RPC_MODULE = 'routers.data_ingest.get_dataset_request'
EXPECTED_RPC_SERVERS = 1

# Every prefix that carries a broker credential or a Kafka connection setting. BROKER_* is the
# Kafka one -- common.kafka.kafka_config reads BROKER_NAME/BROKER_PORT/BROKER_CONN_TIMEOUT at
# import time, and BROKER_PORT is cast to int, which is the exact read that used to explode.
CREDENTIAL_PREFIXES = ('ALPACA_', 'BROKER_', 'KAFKA_')

# Runs in a subprocess, so it may only assume the standard library and the installed packages.
# The environment check comes before any project import, and prints its own marker, so that a
# run which skipped it cannot be mistaken for a run which passed it.
IMPORT_PROBE = """
import os
import sys

PREFIXES = {prefixes!r}
APP_MODULE = {app_module!r}
RPC_MODULE = {rpc_module!r}

leaked = sorted(name for name in os.environ if name.startswith(PREFIXES))
if leaked:
    raise SystemExit('environment was not stripped, still set: ' + ', '.join(leaked))
print('stripped-ok')

if APP_MODULE in sys.modules or RPC_MODULE in sys.modules:
    raise SystemExit('module was already imported, so this proves nothing about import time')

import importlib

app_module = importlib.import_module(APP_MODULE)
rpc_module = importlib.import_module(RPC_MODULE)
print('imported-ok')

from fastapi import FastAPI

if not isinstance(app_module.app, FastAPI):
    raise SystemExit('app is a ' + type(app_module.app).__name__ + ', not a FastAPI instance')

print('rpc-servers=' + str(len(rpc_module.rpc._rpc_servers)))
"""


def probe_env() -> dict[str, str]:
    """Build the whole environment the probe runs under.

    Not os.environ with entries removed: a wholesale replacement cannot be made vacuous later
    by a variable nobody thought to name here. PYTHONPATH is what puts the repo on the path,
    since the child inherits nothing.

    Returns:
        dict[str, str]: Every variable the probe process will see.
    """
    return {'PYTHONPATH': str(REPO_ROOT)}


@pytest.fixture(scope='module')
def import_probe() -> subprocess.CompletedProcess:
    """Import the app module in a fresh interpreter with no credentials in the environment.

    A subprocess rather than importlib.reload: reloading the app module re-executes only that
    module's body, while its already-cached dependencies -- common.kafka.kafka_config among
    them -- keep their first-import values. Those dependencies are where both regressions
    actually lived, so an in-process reload would pass while the bug was present. Module-scoped
    so the whole file costs one fork.
    """
    program = IMPORT_PROBE.format(prefixes=CREDENTIAL_PREFIXES, app_module=APP_MODULE, rpc_module=RPC_MODULE)
    return subprocess.run(
        [sys.executable, '-c', program],
        cwd=REPO_ROOT,
        env=probe_env(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_the_app_module_imports_with_no_credentials_in_the_environment(import_probe):
    assert import_probe.returncode == 0, f'import failed:\n{import_probe.stdout}\n{import_probe.stderr}'
    assert 'imported-ok' in import_probe.stdout


def test_the_probe_really_ran_with_a_stripped_environment(import_probe):
    # Without this the test could pass on a machine that simply had the variables set.
    assert 'stripped-ok' in import_probe.stdout


def test_no_credential_is_handed_to_the_probe_whatever_this_session_inherited():
    # The other half of the same proof, from the parent's side, and the one that keeps holding
    # if someone later widens probe_env(): whatever pytest itself was started with, the child
    # is handed nothing matching a credential prefix.
    assert not [name for name in probe_env() if name.startswith(CREDENTIAL_PREFIXES)]


def test_the_rpc_server_registers_while_the_module_body_runs(import_probe):
    # routers/data_ingest/get_dataset_request.py decorates store_data at module scope. If that
    # line stops running, the service starts and answers nothing -- so the count, not just the
    # absence of an exception, is what this file is guarding.
    assert f'rpc-servers={EXPECTED_RPC_SERVERS}' in import_probe.stdout
