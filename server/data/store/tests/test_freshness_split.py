"""The freshness split: trading_calendar -> completeness -> health, with no back edge and no shim (validator).

Design: tj-grna9p.170 (the symbol map) on the owner's ruling of 2026-10-21 and the pointer addendum on decision
tj-grna9p.8. freshness.py was split into three modules by a PURE MOVE; the behaviour is pinned, unchanged, by the
suites that used to import freshness (test_freshness.py, test_freshness_periods.py, test_coverage_buckets.py,
test_provisional_bar.py and the route suites). This file pins the STRUCTURE the move created, which nothing else
would notice drifting:

* THE IMPORT GRAPH. trading_calendar imports only the standard library, exchange_calendars, pandas and common.enums;
  completeness adds trading_calendar and nothing third-party; health adds completeness. No module imports one above
  it, nothing anywhere imports data.store.app.freshness, and each module imports cleanly in a fresh interpreter
  whatever the order (a cycle shows up as an ImportError in some order, never in all).
* NO SHIM. freshness.py does not exist, so an old import path fails loudly instead of working by accident.
* OWNERSHIP. COMPLETE and GAPS are written once, in completeness.CompletenessStatus; health.FreshnessStatus maps
  them (same string values) and the STATIC branch of evaluate_dataset goes through assess_completeness.
  FreshnessStatus keeps the values the proto enum FreshnessStatus names (tj-grna9p.168 renames both together).
* THE SYMBOL MAP. Every public name of the old module is defined and exported by exactly one new module, the one the
  map gives it; each module's __all__ is exactly its own names.
* THE ENV NAME. FRESHNESS_STREAM_BAR_MULTIPLE is unchanged: renaming it would silently drop an operator's setting.
"""

import ast
import dataclasses
import importlib
import importlib.util
import itertools
import json
import os
import re
import subprocess
import sys
from datetime import UTC, date, datetime

import pytest

from common.enums.data_stock import DataSource, Granularity, UpdateType
from common.tests.roots import SERVER_ROOT
from data.store.app import completeness, health, trading_calendar
from data.store.app.completeness import CompletenessResult, CompletenessStatus, assess_completeness
from data.store.app.health import FreshnessStatus, evaluate_dataset
from data.store.app.trading_calendar import calendar_for_source


APP = SERVER_ROOT / 'data' / 'store' / 'app'
PACKAGE = 'data.store.app'
MODULES = ('trading_calendar', 'completeness', 'health')
OLD = f'{PACKAGE}.freshness'
XNYS = calendar_for_source(DataSource.ALPACA_API)

# The symbol map of tj-grna9p.170, as the builder delivered it: every public name of the old freshness.__all__ (24),
# the four private names made public (_require_aware, _next_period_start, _PERIODIC, _judged), and the two new
# completeness types and its one new function. bar_period_close and last_bar_is_provisional, which the bead's map
# does not name, went to health beside _daily, their one caller.
EXPECTED: dict[str, str] = {
    'CALENDAR_START': 'trading_calendar',
    'SOURCE_CALENDARS': 'trading_calendar',
    'CalendarRangeError': 'trading_calendar',
    'ExchangeCalendarAdapter': 'trading_calendar',
    'NaiveDatetimeError': 'trading_calendar',
    'TradingCalendar': 'trading_calendar',
    'UnknownCalendarError': 'trading_calendar',
    'calendar_for_source': 'trading_calendar',
    'local_midnight': 'trading_calendar',
    'require_aware': 'trading_calendar',
    'session_of': 'trading_calendar',
    'PERIODIC': 'completeness',
    'CompletenessResult': 'completeness',
    'CompletenessStatus': 'completeness',
    'CoverageBucket': 'completeness',
    'GapInterval': 'completeness',
    'assess_completeness': 'completeness',
    'coverage_buckets': 'completeness',
    'covered_sessions': 'completeness',
    'gap_intervals': 'completeness',
    'judged': 'completeness',
    'next_period_start': 'completeness',
    'period_start': 'completeness',
    'DEFAULT_STREAM_BAR_MULTIPLE': 'health',
    'STREAM_BAR_MULTIPLE_ENV': 'health',
    # tj-grna9p.172: the settle margin, the one setting the DAILY refresh and the final-bar rule share. daily_refresh
    # re-exports SETTLE_ENV and DEFAULT_SETTLE_MINUTES; it is not one of the three modules, so no exporter here.
    'SETTLE_ENV': 'health',
    'DEFAULT_SETTLE_MINUTES': 'health',
    'DEFAULT_SETTLE_MARGIN': 'health',
    'settle_margin_from_env': 'health',
    'DatasetHealth': 'health',
    'FreshnessConfig': 'health',
    'FreshnessStatus': 'health',
    'bar_period_close': 'health',
    'evaluate_dataset': 'health',
    'last_bar_is_provisional': 'health',
}

# freshness.__all__ at 9ecbf88, the tip before the split, written out: each must still be importable.
OLD_ALL = (
    'CALENDAR_START',
    'DEFAULT_STREAM_BAR_MULTIPLE',
    'SOURCE_CALENDARS',
    'STREAM_BAR_MULTIPLE_ENV',
    'CalendarRangeError',
    'CoverageBucket',
    'DatasetHealth',
    'ExchangeCalendarAdapter',
    'FreshnessConfig',
    'FreshnessStatus',
    'GapInterval',
    'NaiveDatetimeError',
    'TradingCalendar',
    'UnknownCalendarError',
    'bar_period_close',
    'calendar_for_source',
    'coverage_buckets',
    'covered_sessions',
    'evaluate_dataset',
    'gap_intervals',
    'last_bar_is_provisional',
    'local_midnight',
    'period_start',
    'session_of',
)
# The private names the split made public, old -> new.
MADE_PUBLIC = {
    '_require_aware': 'require_aware',
    '_next_period_start': 'next_period_start',
    '_PERIODIC': 'PERIODIC',
    '_judged': 'judged',
}

# What each module may import. First party is named module by module; third party by top-level name.
ALLOWED_FIRST_PARTY = {
    'trading_calendar': {'common.enums.data_stock'},
    'completeness': {'common.enums.data_stock', f'{PACKAGE}.trading_calendar'},
    'health': {'common.enums.data_stock', f'{PACKAGE}.trading_calendar', f'{PACKAGE}.completeness'},
}
ALLOWED_THIRD_PARTY = {'trading_calendar': {'exchange_calendars', 'pandas'}, 'completeness': set(), 'health': set()}
FIRST_PARTY_TOPS = {'common', 'data', 'routers', 'schemas', 'trader_joe', 'tools', 'tests'}


def _imports(path) -> set[str]:
    """Every module a file imports, at any depth (a function-level import counts too), as absolute dotted names."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert not node.level, f'{path.name}: a relative import ({ast.unparse(node)}) escapes this check'
            found.add(node.module)
            # `from data.store.app import health` imports a module, not a name.
            found.update(f'{node.module}.{alias.name}' for alias in node.names if node.module == PACKAGE)
    return found


# ------------------------------------------------------------------------------------------------ the import graph


@pytest.mark.parametrize('module', MODULES)
def test_each_module_imports_only_what_the_chain_allows(module):
    imported = _imports(APP / f'{module}.py')
    third = {name.partition('.')[0] for name in imported} - FIRST_PARTY_TOPS - set(sys.stdlib_module_names)
    first = {name for name in imported if name.partition('.')[0] in FIRST_PARTY_TOPS}
    assert third == ALLOWED_THIRD_PARTY[module], (module, sorted(third))
    assert first == ALLOWED_FIRST_PARTY[module], (module, sorted(first))


def test_the_chain_has_no_back_edge():
    """trading_calendar imports neither completeness nor health; completeness does not import health."""
    below = {'trading_calendar': ('completeness', 'health'), 'completeness': ('health',), 'health': ()}
    for module, forbidden in below.items():
        imported = _imports(APP / f'{module}.py')
        for upper in forbidden:
            assert f'{PACKAGE}.{upper}' not in imported, f'{module} imports {upper}: a back edge'


def test_freshness_py_is_gone_and_left_no_shim():
    assert not (APP / 'freshness.py').exists()
    assert not (APP / 'freshness').exists()
    assert importlib.util.find_spec(OLD) is None
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(OLD)


def _python_files():
    repo = SERVER_ROOT.parent
    roots = [SERVER_ROOT, repo / 'tools', repo / 'tests']
    for root in roots:
        for path in root.rglob('*.py'):
            parts = path.relative_to(repo).parts
            if {'.claude', '.venv', 'node_modules', '__pycache__'} & set(parts):
                continue
            yield path


def test_nothing_imports_the_old_module():
    """No import of data.store.app.freshness, nor a string naming it (monkeypatch targets, importlib), anywhere."""
    this = os.path.realpath(__file__)
    offenders = []
    for path in _python_files():
        if os.path.realpath(path) == this:
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'))
        offenders.extend(f'{path}:{node.lineno}' for node in ast.walk(tree) if _names_the_old_module(node))
    assert not offenders, offenders


def _is_old(name: str | None) -> bool:
    return name is not None and (name == OLD or name.startswith(f'{OLD}.'))


def _names_the_old_module(node: ast.AST) -> bool:
    """An import of it, or a string that IS its path (a docstring telling the history does not count)."""
    if isinstance(node, ast.Import):
        return any(_is_old(alias.name) for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        from_package = node.module == PACKAGE and any(alias.name == 'freshness' for alias in node.names)
        return _is_old(node.module) or from_package
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and _is_old(node.value)


# The child imports the modules in the given order, then reports what loaded: a fresh interpreter, so nothing the
# parent already imported can hide a cycle.
CHILD = r"""
import importlib, json, sys
order = sys.argv[1:]
loaded_after_first = None
for index, name in enumerate(order):
    importlib.import_module('data.store.app.' + name)
    if index == 0:
        loaded_after_first = sorted(m for m in sys.modules if m.startswith('data.store.app.'))
print(json.dumps({'first': loaded_after_first, 'freshness': 'data.store.app.freshness' in sys.modules}))
"""


def _child(order: tuple[str, ...]) -> dict:
    env = {**os.environ, 'PYTHONPATH': os.pathsep.join(p for p in sys.path if p)}
    done = subprocess.run(  # nosec B603 -- this interpreter, a fixed script, no shell
        [sys.executable, '-c', CHILD, *order],
        capture_output=True,
        text=True,
        env=env,
        cwd=SERVER_ROOT.parent,
        timeout=180,
        check=False,
    )
    assert done.returncode == 0, (order, done.stderr[-4000:])
    return json.loads(done.stdout.strip().splitlines()[-1])


# What importing each module alone may load from the package: itself and what it sits on, never what sits on it.
LOADS = {
    'trading_calendar': {'data.store.app.trading_calendar'},
    'completeness': {'data.store.app.trading_calendar', 'data.store.app.completeness'},
    'health': {'data.store.app.trading_calendar', 'data.store.app.completeness', 'data.store.app.health'},
}


@pytest.mark.parametrize('order', list(itertools.permutations(MODULES)), ids='-'.join)
def test_each_order_imports_cleanly_in_a_fresh_interpreter(order):
    report = _child(order)
    assert set(report['first']) == LOADS[order[0]], report
    assert report['freshness'] is False


# ------------------------------------------------------------------------------------------------------ ownership


def _class_body(module: str, name: str) -> dict[str, ast.expr]:
    tree = ast.parse((APP / f'{module}.py').read_text(encoding='utf-8'))
    (cls,) = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name]
    return {s.targets[0].id: s.value for s in cls.body if isinstance(s, ast.Assign)}


def test_complete_and_gaps_are_written_once_in_completeness():
    body = _class_body('completeness', 'CompletenessStatus')
    assert {k: v.value for k, v in body.items()} == {'COMPLETE': 'COMPLETE', 'GAPS': 'GAPS'}
    assert [m.value for m in CompletenessStatus] == ['COMPLETE', 'GAPS']


def test_health_maps_complete_and_gaps_instead_of_redefining_them():
    body = _class_body('health', 'FreshnessStatus')
    for name in ('COMPLETE', 'GAPS'):
        assert ast.unparse(body[name]) == f'CompletenessStatus.{name}.value', (name, ast.unparse(body[name]))
    # Only the four statuses completeness does not own are literals in health.
    literals = {k for k, v in body.items() if isinstance(v, ast.Constant)}
    assert literals == {'FRESH', 'LATE', 'OVERDUE', 'RETIRED'}
    for member in CompletenessStatus:
        assert FreshnessStatus[member.name].value == member.value


@pytest.mark.parametrize(
    ('verdict', 'gap_count', 'status'),
    [
        (CompletenessResult(CompletenessStatus.GAPS, 7), 7, FreshnessStatus.GAPS),
        (CompletenessResult(CompletenessStatus.COMPLETE), 0, FreshnessStatus.COMPLETE),
    ],
)
def test_the_static_branch_goes_through_assess_completeness(monkeypatch, verdict, gap_count, status):
    """A verdict no real range produces (7 gaps over a range that has none) can only come from the stub."""
    seen = []

    def stub(*args):
        seen.append(args)
        return verdict

    monkeypatch.setattr(health, 'assess_completeness', stub)
    start, end = datetime(2026, 10, 5, 4, tzinfo=UTC), datetime(2026, 10, 7, 4, tzinfo=UTC)
    now = datetime(2026, 10, 20, tzinfo=UTC)
    covered = frozenset({date(2026, 10, 5), date(2026, 10, 6)})
    got = evaluate_dataset(
        update_type=UpdateType.STATIC,
        granularity=Granularity.ONE_DAY,
        collect=None,
        start=start,
        end=end,
        last_bar=None,
        covered=covered,
        calendar=XNYS,
        now=now,
    )
    assert (got.status, got.gap_count) == (status, gap_count)
    assert type(got.status) is FreshnessStatus
    assert seen == [(start, end, covered, Granularity.ONE_DAY, XNYS, now)]


@pytest.mark.parametrize(
    ('covered', 'expected'),
    [
        # Mon 5 .. Fri 9 Oct 2026, all five sessions held.
        (frozenset(date(2026, 10, d) for d in range(5, 10)), CompletenessResult(CompletenessStatus.COMPLETE, 0)),
        # Wednesday missing.
        (frozenset(date(2026, 10, d) for d in (5, 6, 8, 9)), CompletenessResult(CompletenessStatus.GAPS, 1)),
        # None held.
        (frozenset(), CompletenessResult(CompletenessStatus.GAPS, 5)),
    ],
)
def test_assess_completeness_agrees_with_evaluate_dataset(covered, expected):
    start, end = datetime(2026, 10, 5, 4, tzinfo=UTC), datetime(2026, 10, 10, 4, tzinfo=UTC)
    now = datetime(2026, 10, 20, tzinfo=UTC)
    got = assess_completeness(start, end, covered, Granularity.ONE_DAY, XNYS, now)
    assert got == expected
    health_of = evaluate_dataset(
        update_type=UpdateType.STATIC,
        granularity=Granularity.ONE_DAY,
        collect=None,
        start=start,
        end=end,
        last_bar=None,
        covered=covered,
        calendar=XNYS,
        now=now,
    )
    assert (health_of.status.value, health_of.gap_count) == (got.status.value, got.gap_count)


def test_completeness_result_is_a_frozen_two_field_record():
    assert dataclasses.is_dataclass(CompletenessResult)
    assert [f.name for f in dataclasses.fields(CompletenessResult)] == ['status', 'gap_count']
    result = CompletenessResult(CompletenessStatus.COMPLETE)
    assert result.gap_count == 0
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.gap_count = 3  # type: ignore[misc]
    assert hash(result) == hash(CompletenessResult(CompletenessStatus.COMPLETE, 0))


def test_freshness_status_keeps_its_members_and_the_proto_names():
    assert [(m.name, m.value) for m in FreshnessStatus] == [
        ('FRESH', 'FRESH'),
        ('LATE', 'LATE'),
        ('OVERDUE', 'OVERDUE'),
        ('COMPLETE', 'COMPLETE'),
        ('GAPS', 'GAPS'),
        ('RETIRED', 'RETIRED'),
    ]
    # Read from the .proto source: a test may not import trader_joe.proto (TID251, ADR tj-8konfu D3).
    source = (SERVER_ROOT.parent / 'proto' / 'trader_joe' / 'proto' / 'ui' / 'v1' / 'data.proto').read_text()
    (block,) = re.findall(r'enum FreshnessStatus \{(.*?)\}', source, flags=re.S)
    proto = set(re.findall(r'^\s*(FRESHNESS_STATUS_[A-Z_]+)\s*=', block, flags=re.M)) - {'FRESHNESS_STATUS_UNSPECIFIED'}
    assert len(proto) == 6, proto
    assert {f'FRESHNESS_STATUS_{m.value}' for m in FreshnessStatus} == proto


# ------------------------------------------------------------------------------------------------- the symbol map


@pytest.mark.parametrize('module', MODULES)
def test_each_module_exports_exactly_its_own_names(module):
    mod = importlib.import_module(f'{PACKAGE}.{module}')
    assert sorted(mod.__all__) == sorted(n for n, owner in EXPECTED.items() if owner == module)
    assert len(mod.__all__) == len(set(mod.__all__))
    for name in mod.__all__:
        assert hasattr(mod, name), (module, name)


@pytest.mark.parametrize('name', sorted(EXPECTED))
def test_each_symbol_is_defined_and_exported_by_exactly_one_module(name):
    owner = EXPECTED[name]
    exporters = [m for m in MODULES if name in importlib.import_module(f'{PACKAGE}.{m}').__all__]
    assert exporters == [owner], (name, exporters)
    # DEFINED there: a module-level assignment, def or class in the owner's source, and in no other module's.
    definers = []
    for m in MODULES:
        tree = ast.parse((APP / f'{m}.py').read_text(encoding='utf-8'))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name == name:
                definers.append(m)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                    definers.append(m)
    assert definers == [owner], (name, definers)
    obj = getattr(importlib.import_module(f'{PACKAGE}.{owner}'), name)
    if hasattr(obj, '__module__') and (isinstance(obj, type) or callable(obj)):
        assert obj.__module__ == f'{PACKAGE}.{owner}', (name, obj.__module__)


def test_every_old_public_name_is_importable_from_its_new_home():
    for name in OLD_ALL:
        assert hasattr(importlib.import_module(f'{PACKAGE}.{EXPECTED[name]}'), name), name
    for old, new in MADE_PUBLIC.items():
        owner = importlib.import_module(f'{PACKAGE}.{EXPECTED[new]}')
        assert hasattr(owner, new), new
        # Renamed, not aliased: the underscore name is gone from every module.
        assert not any(hasattr(importlib.import_module(f'{PACKAGE}.{m}'), old) for m in MODULES), old


def test_the_stream_bar_multiple_env_name_is_unchanged():
    assert health.STREAM_BAR_MULTIPLE_ENV == 'FRESHNESS_STREAM_BAR_MULTIPLE'
    config = health.FreshnessConfig.from_env({'FRESHNESS_STREAM_BAR_MULTIPLE': '5'})
    assert config.stream_bar_multiple == 5
    assert health.FreshnessConfig.from_env({}).stream_bar_multiple == health.DEFAULT_STREAM_BAR_MULTIPLE == 3


def test_the_modules_are_the_ones_under_test():
    """The imports above resolve to the files this test parses, not to an installed copy elsewhere."""
    for mod in (trading_calendar, completeness, health):
        assert os.path.realpath(mod.__file__) == os.path.realpath(APP / f'{mod.__name__.rpartition(".")[2]}.py')
