"""build_infra pin: tests/system collects with the vendor SDK absent, as it must in the client image (tj-7zjov1.304).

The system suite runs from the test_client container, whose image (Dockerfile stage system_test_image) installs no
service dependency group, so the alpaca SDK is not there (ADR tj-q9ae5u addendum 1 item 4'). The dev venv DOES have
it, so a tests/system import that reaches the SDK -- directly, or through an innocent-looking constant in a module
that imports broker_api -- collects cleanly here and fails only at a stack session or in CI's client steps. That
happened twice; the second time was DEFAULT_MAX_ATTEMPTS, imported from brokers/alpaca/handle.py until tj-7zjov1.303
moved it to scheduler/retry.py.

This module runs `pytest --collect-only -q tests/system` in a SUBPROCESS whose first act, before pytest or anything
else is imported, is to put a finder at the front of sys.meta_path that raises ImportError for `alpaca` and every
`alpaca.*` submodule. It holds:

  1. COLLECTION SUCCEEDS WITH THE SDK BLOCKED: exit status 0 (never the summary text alone: pytest.ini's
     --continue-on-collection-errors lets a run print a count with an error above it), no `error` in the summary,
     and the blocker's own message nowhere in the output.
  2. BLOCKING LOSES NO TEST: the blocked run collects exactly as many tests as the same run with nothing blocked. A
     module that swallowed the ImportError and defined fewer tests would pass point 1 and fail here.
  3. THE BLOCKER IS NOT VACUOUS: the SDK is installed in this venv, and a child with the same bootstrap that imports
     an alpaca submodule fails with the blocker's message. Without this, a finder that never fired would pass 1 and 2.

The child's environment and import path are test_system_suite_collects's (_child_env): the env contract stubbed
with dummy values, PYTHONPATH as the test_client container sets it. The child is NOT started with `python -I`: -I
discards PYTHONPATH, which is exactly the import path the container uses and the one this check must mirror.

Collect-only reaches module-level imports only. An SDK import inside a fixture or test body is not caught here; it
would fail at the system tier with the SDK missing, which is where a body runs.
"""

import importlib.util
import subprocess
import sys

import pytest

from common.tests.roots import REPO_ROOT
from common.tests.test_system_suite_collects import COLLECT_TIMEOUT_SECONDS, SUMMARY, SYSTEM_ARG, _child_env, _shown


pytestmark = pytest.mark.build_infra

VENDOR_PACKAGE = 'alpaca'
BLOCKED_MESSAGE = 'BLOCKED VENDOR SDK (test_system_tier_sdk_free)'

# Runs first in the child, so the finder is in place before any module is imported.
BLOCKER = f"""
import sys


class _BlockVendorSdk:
    def find_spec(self, name, path=None, target=None):
        if name == {VENDOR_PACKAGE!r} or name.startswith({VENDOR_PACKAGE + '.'!r}):
            raise ImportError({BLOCKED_MESSAGE!r} + ': ' + name)
        return None


sys.meta_path.insert(0, _BlockVendorSdk())
"""

COLLECT = f"""
import pytest

sys.exit(pytest.main(['--collect-only', '-q', '-p', 'no:cacheprovider', {SYSTEM_ARG!r}]))
"""


def _run_child(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, '-c', code],
        cwd=REPO_ROOT,
        env=_child_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=COLLECT_TIMEOUT_SECONDS,
    )


@pytest.fixture(scope='module')
def blocked_collection() -> subprocess.CompletedProcess:
    """One collect-only run of the system suite with the vendor SDK blocked."""
    return _run_child(BLOCKER + COLLECT)


@pytest.fixture(scope='module')
def open_collection() -> subprocess.CompletedProcess:
    """The same run with nothing blocked: the count the blocked run must match."""
    return _run_child('import sys\n' + COLLECT)


def _collected(result: subprocess.CompletedProcess) -> int:
    summary = SUMMARY.search(result.stdout)
    assert summary, f'no collected-count summary line\n{_shown(result)}'
    return int(summary.group(1))


def test_the_system_suite_collects_with_the_vendor_sdk_blocked(blocked_collection):
    output = blocked_collection.stdout + blocked_collection.stderr
    assert BLOCKED_MESSAGE not in output, (
        f'a module tests/system collects imports the {VENDOR_PACKAGE} SDK, which the client image does not install; '
        f'import the name from an SDK-free module instead\n{_shown(blocked_collection)}'
    )
    assert blocked_collection.returncode == 0, _shown(blocked_collection)
    summary = SUMMARY.search(blocked_collection.stdout)
    assert summary, f'no collected-count summary line\n{_shown(blocked_collection)}'
    assert 'error' not in summary.group(0).lower(), _shown(blocked_collection)


def test_blocking_the_vendor_sdk_loses_no_test(blocked_collection, open_collection):
    assert open_collection.returncode == 0, _shown(open_collection)
    assert _collected(blocked_collection) == _collected(open_collection), _shown(blocked_collection)


def test_the_vendor_sdk_is_installed_here():
    # The guard means something only where the SDK exists: absent, nothing could reach it and point 1 is free.
    assert importlib.util.find_spec(VENDOR_PACKAGE) is not None


@pytest.mark.parametrize('module', [VENDOR_PACKAGE, f'{VENDOR_PACKAGE}.common.constants'])
def test_the_blocker_refuses_the_vendor_sdk(module: str):
    result = _run_child(BLOCKER + f'import {module}\n')
    assert result.returncode != 0, _shown(result)
    assert f'{BLOCKED_MESSAGE}: {VENDOR_PACKAGE}' in result.stderr, _shown(result)
