import subprocess
import sys
from pathlib import Path

import pytest

from common.tests.image_path import image_pythonpath


# The guard this file exists for (tj-8yix3i): twice now a module-scope dependency -- first the
# Alpaca client, then the Kafka producer -- made `import data.ingest.app.main` raise, and the
# suite stayed green because nothing anywhere imports an app main module. uvicorn imports it in
# a fresh interpreter at startup, so that is what this reproduces.
#
# To mirror this for another service, copy the file into that service's tests and change
# APP_MODULE and ROUTER_MODULE -- nothing else.
REPO_ROOT = Path(__file__).resolve().parents[3]
APP_MODULE = 'data.ingest.app.main'
# Imported by name as well as through the app, so an import-time break in the router survives
# main.py one day not mounting it. It is routers/data_ingest's only module with a module body worth
# running: until tj-3mk3u5.11 it built a KafkaRpcFactory and decorated store_data at import, and
# the probe counted the registrations that produced. Both are gone -- see the retirement note on
# the probe below -- and what is left is an empty APIRouter that must still import cleanly.
ROUTER_MODULE = 'routers.data_ingest.get_dataset_request'

# Every prefix that carries a broker credential or a connection setting, used ONLY as a negative:
# the probe refuses to run if the environment it was handed still has any of them set.
#
# BROKER_* and KAFKA_* STAY, with Kafka unwired (tj-3mk3u5.11). They named the read that used to
# explode -- common.kafka.kafka_config cast BROKER_PORT to int at import -- and that module is no
# longer in this app's closure, so the justification is gone but the prefixes are not. They cost
# nothing and they keep the scan honest if one ever comes back, which is exactly why ALPACA_ is in
# data_store's copy of this file, matching nothing there. A prefix this service DOES read, omitted,
# is what would make the assertions below vacuous; a prefix it does not read cannot.
CREDENTIAL_PREFIXES = ('ALPACA_', 'BROKER_', 'KAFKA_')

# Runs in a subprocess, so it may only assume the standard library and the installed packages.
# The environment check comes before any project import, and prints its own marker, so that a
# run which skipped it cannot be mistaken for a run which passed it.
IMPORT_PROBE = """
import os
import sys

PREFIXES = {prefixes!r}
APP_MODULE = {app_module!r}
ROUTER_MODULE = {router_module!r}

leaked = sorted(name for name in os.environ if name.startswith(PREFIXES))
if leaked:
    raise SystemExit('environment was not stripped, still set: ' + ', '.join(leaked))
print('stripped-ok')

if APP_MODULE in sys.modules or ROUTER_MODULE in sys.modules:
    raise SystemExit('module was already imported, so this proves nothing about import time')

import importlib

app_module = importlib.import_module(APP_MODULE)
importlib.import_module(ROUTER_MODULE)
print('imported-ok')

from fastapi import FastAPI

if not isinstance(app_module.app, FastAPI):
    raise SystemExit('app is a ' + type(app_module.app).__name__ + ', not a FastAPI instance')
"""


def probe_env() -> dict[str, str]:
    """Build the whole environment the probe runs under.

    Not os.environ with entries removed: a wholesale replacement cannot be made vacuous later
    by a variable nobody thought to name here. PYTHONPATH is what puts the repo on the path,
    since the child inherits nothing: the image's two entries, the root and the generated gRPC
    code's root (common/tests/image_path.py, decision tj-3mk3u5.42 F1), as uvicorn gets them.

    Returns:
        dict[str, str]: Every variable the probe process will see.
    """
    return {'PYTHONPATH': image_pythonpath(REPO_ROOT)}


@pytest.fixture(scope='module')
def import_probe() -> subprocess.CompletedProcess:
    """Import the app module in a fresh interpreter with no credentials in the environment.

    A subprocess rather than importlib.reload: reloading the app module re-executes only that
    module's body, while its already-cached dependencies -- common.kafka.kafka_config among
    them -- keep their first-import values. Those dependencies are where both regressions
    actually lived, so an in-process reload would pass while the bug was present. Module-scoped
    so the whole file costs one fork.
    """
    program = IMPORT_PROBE.format(prefixes=CREDENTIAL_PREFIXES, app_module=APP_MODULE, router_module=ROUTER_MODULE)
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


# RETIRED ON tj-3mk3u5.11: test_the_rpc_server_registers_while_the_module_body_runs, and the
# EXPECTED_RPC_SERVERS constant and `rpc-servers=` probe line it read.
#
# It asserted that get_dataset_request.py's module body had really decorated store_data with
# @rpc.add_server(...), because a decorator that silently stopped running left a service that
# starts and answers nothing -- the count, not merely the absence of an exception, was the guard.
# tj-3mk3u5.11 deletes the factory, the decorator and store_data, so there is no registration left
# to count and the probe line raised AttributeError instead of asserting anything.
#
# THE INVARIANT IT STOOD FOR DID NOT GO, IT MOVED, and it is pinned twice over. data_ingest's real
# surface is the gRPC IngestService: test_grpc_host.py asserts in a fresh interpreter that
# registered_services() returns exactly that service, which is the same claim -- the surface equals
# a committed expectation, and a registration that stopped happening reds. The HTTP side is held by
# the `none` declaration in routers/tests/interface_manifest/data_ingest.manifest, which still reds
# if a route appears (decision tj-3wgh03 D4: each surface is pinned where it lives).
#
# THIS FILE'S OWN GUARD IS UNTOUCHED and is why it is not deleted: a module-scope dependency that
# makes `import data.ingest.app.main` raise is still caught above, in a fresh interpreter, with the
# credentials stripped. That is what it was written for (tj-8yix3i) and it has caught two real
# regressions.
#
# FOUND BY tj-3mk3u5.11's OWN TEST RUN, not by the retirement pass that should have caught it:
# tj-3mk3u5.32 retired every other Kafka-transport assertion ahead of this commit and missed this
# file. One file, found by the next commit, which is when it should be found.
