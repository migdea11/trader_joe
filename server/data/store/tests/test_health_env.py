"""HEALTH_STREAM_BAR_MULTIPLE is read; the former FRESHNESS_STREAM_BAR_MULTIPLE is not (validator, tj-7zjov1.223).

DESIGN: tj-grna9p.168 (owner ruling 2026-10-30, option B: no 'freshness' for dataset state; NAME TABLE: env renamed,
no alias) and the owner ruling of 2026-11-02 on tj-7zjov1.222: NO fallback for the old name, since there is no
production release yet. The one-release fallback of tj-7zjov1.202 and its warning are removed. What is pinned:
    (1) the new name alone is read; neither name set is the default (3);
    (2) the old name alone is NOT read: the default, nothing logged, and its value is not even parsed;
    (3) both set: the new one wins and the old one is ignored;
    (4) an invalid new value is refused as before (from_env parses with int(), HealthConfig refuses < 1);
    (5) the same through os.environ in a fresh interpreter, as the service reads it, with nothing logged;
    (6) health imports no logging at all, first party or stdlib;
    (7) no store or data_store router production module says 'freshness' for dataset state, with NO allowance: the
        legacy env name was the only one, and it is gone. An allowance must still match, so one added later and left
        stale fails here.
"""

import ast
import logging
import os
import re
import subprocess
import sys

import pytest

from common.tests.roots import SERVER_ROOT
from data.store.app import health
from data.store.app.health import DEFAULT_STREAM_BAR_MULTIPLE, HealthConfig


NEW = 'HEALTH_STREAM_BAR_MULTIPLE'
OLD = 'FRESHNESS_STREAM_BAR_MULTIPLE'
HEALTH_PY = SERVER_ROOT / 'data' / 'store' / 'app' / 'health.py'


def test_the_name_is_the_ruled_one():
    assert health.STREAM_BAR_MULTIPLE_ENV == NEW
    assert DEFAULT_STREAM_BAR_MULTIPLE == 3


def test_neither_name_set_is_the_default(caplog):
    caplog.set_level(logging.DEBUG)
    assert HealthConfig.from_env({}).stream_bar_multiple == 3
    assert caplog.records == []


def test_the_new_name_alone_is_read(caplog):
    caplog.set_level(logging.DEBUG)
    assert HealthConfig.from_env({NEW: '5'}).stream_bar_multiple == 5
    assert caplog.records == []


@pytest.mark.parametrize('raw', ['6', '1', '0', '-2', 'not a number', ''])
def test_the_old_name_alone_is_not_read_and_nothing_is_logged(caplog, raw):
    # '0', '-2', '' and a non-number would raise if the value were parsed: the old name is never looked at.
    caplog.set_level(logging.DEBUG)
    assert HealthConfig.from_env({OLD: raw}).stream_bar_multiple == 3
    assert caplog.records == [], [r.getMessage() for r in caplog.records]


def test_both_set_the_new_name_wins_and_the_old_is_ignored(caplog):
    caplog.set_level(logging.DEBUG)
    assert HealthConfig.from_env({NEW: '4', OLD: '9'}).stream_bar_multiple == 4
    assert HealthConfig.from_env({NEW: '4', OLD: 'not a number'}).stream_bar_multiple == 4
    assert caplog.records == []


@pytest.mark.parametrize('raw', ['0', '-1'])
def test_a_new_value_below_one_is_refused(raw):
    with pytest.raises(ValueError, match='stream_bar_multiple'):
        HealthConfig.from_env({NEW: raw})


@pytest.mark.parametrize('raw', ['not a number', '', '2.5'])
def test_a_malformed_new_value_is_refused(raw):
    # int() refuses it; there is no silent default for a set but malformed new name.
    with pytest.raises(ValueError):
        HealthConfig.from_env({NEW: raw})


def test_a_malformed_new_value_is_refused_even_beside_a_valid_old_one():
    with pytest.raises(ValueError):
        HealthConfig.from_env({NEW: 'not a number', OLD: '5'})


# A fresh interpreter: the service's own path (environ omitted, so os.environ), every logger at DEBUG to stderr.
CHILD = r"""
import logging, sys
logging.basicConfig(stream=sys.stderr, level=logging.DEBUG, format='LOG %(name)s %(message)s', force=True)
from data.store.app.health import HealthConfig
print(','.join(str(HealthConfig.from_env().stream_bar_multiple) for _ in range(3)))
"""


def _child(environ: dict[str, str]) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in (NEW, OLD)}
    env.update(environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    return subprocess.run(  # nosec B603 -- this interpreter, a fixed script, no shell
        [sys.executable, '-c', CHILD],
        capture_output=True,
        text=True,
        env=env,
        cwd=SERVER_ROOT.parent,
        timeout=180,
        check=False,
    )


@pytest.mark.parametrize(
    ('environ', 'values'),
    [({OLD: '6'}, '3,3,3'), ({NEW: '5'}, '5,5,5'), ({NEW: '5', OLD: '6'}, '5,5,5'), ({}, '3,3,3')],
    ids=['old-only', 'new-only', 'both', 'neither'],
)
def test_through_os_environ_only_the_new_name_is_read(environ, values):
    done = _child(environ)
    assert done.returncode == 0, done.stderr[-4000:]
    assert done.stdout.strip().splitlines()[-1] == values
    assert not [line for line in done.stderr.splitlines() if line.startswith(f'LOG {health.__name__} ')], done.stderr
    assert OLD not in done.stderr, done.stderr


def test_health_imports_no_logging():
    imported: set[str] = set()
    for node in ast.walk(ast.parse(HEALTH_PY.read_text(encoding='utf-8'))):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f'{node.module}.{alias.name}' for alias in node.names)
    logging_imports = sorted(name for name in imported if 'logging' in name.split('.'))
    assert logging_imports == [], logging_imports


# ---------------------------------------------------------------------- no 'freshness' for dataset state (pin 7)

PRODUCTION_ROOTS = (SERVER_ROOT / 'data' / 'store', SERVER_ROOT / 'routers' / 'data_store')

# (path relative to server/, the exact stripped line) -> why it may say freshness. EMPTY: the legacy env name was the
# only allowance, and it went with the fallback (tj-7zjov1.222). Generic English ("data freshness" meaning something
# other than dataset state) would go here, with its reason; every entry must still match a line.
ALLOWED: dict[tuple[str, str], str] = {}


def _production_files():
    for root in PRODUCTION_ROOTS:
        for path in sorted(root.rglob('*.py')):
            parts = set(path.relative_to(SERVER_ROOT).parts)
            if parts & {'tests', '__pycache__'}:
                continue
            yield path


def test_the_scan_reaches_the_renamed_modules():
    names = {path.relative_to(SERVER_ROOT).as_posix() for path in _production_files()}
    for expected in (
        'data/store/app/health.py',
        'data/store/app/dataset_catalog.py',
        'data/store/app/daily_refresh.py',
        'routers/data_store/ui_mapping.py',
        'routers/data_store/ui_datasets.py',
        'routers/data_store/ui_dataset_writes.py',
    ):
        assert expected in names, expected


def test_no_store_or_router_production_module_says_freshness():
    word = re.compile('freshness', re.IGNORECASE)
    offenders, used = [], set()
    for path in _production_files():
        relative = path.relative_to(SERVER_ROOT).as_posix()
        for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), start=1):
            if not word.search(line):
                continue
            key = (relative, line.strip())
            if key in ALLOWED:
                used.add(key)
            else:
                offenders.append(f'{relative}:{number}: {line.strip()}')
    assert not offenders, offenders
    assert used == set(ALLOWED), f'an allowance no longer matches; remove it: {sorted(set(ALLOWED) - used)}'
