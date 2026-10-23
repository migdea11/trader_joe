"""Every call to a repository function or dataclass names only parameters its definition has, and all it requires.

tj-grna9p.180: builder commit 25b38c1 renamed evaluate_dataset's `expiry` keyword to `collect`
(server/data/store/app/health.py). Every caller in the regular suite moved with it; one call site in
tests/system/test_daily_refresh_pass.py (the `_health` helper) did not, and failed with a TypeError
only when the system suite ran on real Postgres. tests/system is outside `make test` by directory
(pytest.ini norecursedirs), so nothing in the PR gate imports, let alone runs, that file: a renamed or
removed parameter there is invisible until a live run.

This check reads the source with ast, so it reaches the system tier without importing it. For each
file under tests/ and server/ it resolves `from <module> import <name>` (and `import <module> as m`,
`from <package> import <module>`) to a file in this repository, and checks every call to a TOP-LEVEL
function or a plain @dataclass (no bases, no own __init__) defined there:

1. every keyword is a parameter of the definition (unless it takes **kwargs);
2. no more positional arguments than it accepts (unless it takes *args);
3. every parameter without a default is given, positionally or by keyword.

A call spreading *args or **kwargs is not judged. Methods, classes that are not plain dataclasses,
and names bound any other way are not resolved: this is a static pin for the signature-rename class,
not a type checker. A call that is wrong ON PURPOSE (a test that the TypeError is raised) carries
`# type: ignore[call-arg]` on its line, the same marker the type checker already needs there.
"""

import ast
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest

from common.tests.roots import REPO_ROOT, SERVER_ROOT, repo_relative


pytestmark = pytest.mark.build_infra

SCANNED = (REPO_ROOT / 'tests', SERVER_ROOT)
# Module roots an absolute import resolves against: the service packages (data, common, ...) and the
# repository's own (tests, tools).
IMPORT_ROOTS = (SERVER_ROOT, REPO_ROOT)
SKIPPED_PARTS = frozenset({'.claude', 'node_modules', 'gen', '__pycache__'})
DELIBERATE = 'type: ignore[call-arg]'

# The call site that motivated this module, so a resolver change that silently stops seeing it fails.
MOTIVATING_FILE = REPO_ROOT / 'tests/system/test_daily_refresh_pass.py'
MOTIVATING_TARGET = 'data.store.app.health.evaluate_dataset'
# Far below today's count (about 4,800); a scan that resolves almost nothing is broken, not clean.
MIN_CHECKED_CALLS = 1000


@dataclass(frozen=True)
class Signature:
    positional: tuple[str, ...]
    keyword_only: tuple[str, ...]
    required: frozenset[str]
    varargs: bool
    varkw: bool


def _function_signature(args: ast.arguments) -> Signature:
    positional = tuple(a.arg for a in args.posonlyargs + args.args)
    keyword_only = tuple(a.arg for a in args.kwonlyargs)
    without_default = positional[: len(positional) - len(args.defaults)]
    required = set(without_default) | {
        a.arg for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True) if d is None
    }
    return Signature(positional, keyword_only, frozenset(required), args.vararg is not None, args.kwarg is not None)


def _is_dataclass_decorator(node: ast.expr) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    return (isinstance(target, ast.Name) and target.id == 'dataclass') or (
        isinstance(target, ast.Attribute) and target.attr == 'dataclass'
    )


def _dataclass_signature(node: ast.ClassDef) -> Signature | None:
    if node.bases or not any(_is_dataclass_decorator(d) for d in node.decorator_list):
        return None
    if any(isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and m.name == '__init__' for m in node.body):
        return None
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Call) and any(k.arg in ('init', 'kw_only') for k in decorator.keywords):
            return None  # init=False or kw_only change the constructor: not judged
    fields: list[str] = []
    required: set[str] = set()
    for member in node.body:
        if not (isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name)):
            continue
        annotation = ast.unparse(member.annotation)
        if 'KW_ONLY' in annotation or 'InitVar' in annotation:
            return None  # the constructor is not the field list: not judged
        if 'ClassVar' in annotation:
            continue
        if member.value is not None and 'init=False' in ast.unparse(member.value):
            continue
        fields.append(member.target.id)
        if member.value is None:
            required.add(member.target.id)
    return Signature(tuple(fields), (), frozenset(required), False, False)


@cache
def definitions(path: Path) -> dict[str, Signature]:
    """The top-level functions and plain dataclasses of one module, by name."""
    found: dict[str, Signature] = {}
    for node in ast.parse(path.read_text(encoding='utf-8')).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[node.name] = _function_signature(node.args)
        elif isinstance(node, ast.ClassDef):
            signature = _dataclass_signature(node)
            if signature is not None:
                found[node.name] = signature
    return found


def resolve(module: str) -> Path | None:
    """The repository file an absolute module name imports, or None for a third-party or generated one."""
    for root in IMPORT_ROOTS:
        base = root.joinpath(*module.split('.'))
        if base.with_suffix('.py').is_file():
            return base.with_suffix('.py')
        if (base / '__init__.py').is_file():
            return base / '__init__.py'
    return None


def problems_of(call: ast.Call, signature: Signature) -> list[str]:
    """What is wrong with one call against its definition: empty when nothing is."""
    if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
        return []
    found: list[str] = []
    accepted = set(signature.positional) | set(signature.keyword_only)
    if not signature.varkw:
        found += [f'unknown keyword {k.arg!r}' for k in call.keywords if k.arg not in accepted]
    if not signature.varargs and len(call.args) > len(signature.positional):
        found.append(f'{len(call.args)} positional arguments, accepts {len(signature.positional)}')
    given = set(signature.positional[: len(call.args)]) | {k.arg for k in call.keywords}
    found += [
        f'missing required {name!r}' for name in sorted(signature.required - given) if name not in ('self', 'cls')
    ]
    return found


@dataclass(frozen=True)
class Finding:
    where: str
    target: str
    problem: str


def scan_source(source: str, where: str) -> tuple[int, list[Finding], set[str]]:
    """Check one module's calls: how many were judged, what is wrong, and which targets were reached."""
    tree = ast.parse(source)
    lines = source.splitlines()
    functions: dict[str, tuple[str, Signature]] = {}
    modules: dict[str, tuple[str, Path]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            source_file = resolve(node.module)
            if source_file is None:
                continue
            for alias in node.names:
                bound = alias.asname or alias.name
                signature = definitions(source_file).get(alias.name)
                if signature is not None:
                    functions[bound] = (f'{node.module}.{alias.name}', signature)
                elif (submodule := resolve(f'{node.module}.{alias.name}')) is not None:
                    modules[bound] = (f'{node.module}.{alias.name}', submodule)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname and (source_file := resolve(alias.name)) is not None:
                    modules[alias.asname] = (alias.name, source_file)
    checked = 0
    findings: list[Finding] = []
    reached: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target: tuple[str, Signature] | None = None
        if isinstance(node.func, ast.Name):
            target = functions.get(node.func.id)
        elif isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            module = modules.get(node.func.value.id)
            if module is not None and (signature := definitions(module[1]).get(node.func.attr)) is not None:
                target = (f'{module[0]}.{node.func.attr}', signature)
        if target is None:
            continue
        if any(DELIBERATE in lines[n - 1] for n in range(node.lineno, (node.end_lineno or node.lineno) + 1)):
            continue
        checked += 1
        reached.add(target[0])
        findings += [Finding(f'{where}:{node.lineno}', target[0], p) for p in problems_of(node, target[1])]
    return checked, findings, reached


def scanned_files() -> list[Path]:
    files: set[Path] = set()
    for top in SCANNED:
        files.update(p for p in top.rglob('*.py') if not SKIPPED_PARTS.intersection(p.relative_to(top).parts))
    return sorted(files)


@cache
def scan_repository() -> tuple[int, tuple[Finding, ...], dict[Path, frozenset[str]]]:
    checked = 0
    findings: list[Finding] = []
    reached: dict[Path, frozenset[str]] = {}
    for path in scanned_files():
        count, found, targets = scan_source(path.read_text(encoding='utf-8'), str(repo_relative(path)))
        checked += count
        findings += found
        reached[path] = frozenset(targets)
    return checked, tuple(findings), reached


def test_every_resolved_call_matches_its_definition():
    _, findings, _ = scan_repository()
    assert not findings, '\n'.join(f'{f.where}: {f.target}: {f.problem}' for f in findings)


def test_the_scan_reaches_the_system_tier_and_the_motivating_call():
    checked, _, reached = scan_repository()
    assert checked >= MIN_CHECKED_CALLS, f'only {checked} calls resolved: the scan is broken, not clean'
    assert MOTIVATING_FILE in reached, 'tests/system is not scanned'
    assert MOTIVATING_TARGET in reached[MOTIVATING_FILE], 'the evaluate_dataset call in the system tier is not seen'


# The defect itself, and its neighbours, against the real definition of evaluate_dataset: each must be named.
_HEALTH_CALL = """
from data.store.app.health import evaluate_dataset

evaluate_dataset(update_type=u, granularity=g, {collect}start=s, end=e, last_bar=b, covered=None, calendar=c, now=n)
"""


@pytest.mark.parametrize(
    ('collect', 'expected'),
    [
        pytest.param('expiry=x, ', ["unknown keyword 'expiry'", "missing required 'collect'"], id='the-defect'),
        pytest.param('collect=x, expiry=x, ', ["unknown keyword 'expiry'"], id='an-extra-keyword'),
        pytest.param('', ["missing required 'collect'"], id='a-missing-required'),
        pytest.param('collect=x, ', [], id='the-fix'),
    ],
)
def test_a_call_to_evaluate_dataset_is_judged_against_its_definition(collect, expected):
    checked, findings, _ = scan_source(_HEALTH_CALL.format(collect=collect), 'probe.py')
    assert checked == 1
    assert [f.problem for f in findings] == expected


def test_positional_overflow_and_the_deliberate_marker():
    source = (
        'from data.store.app.health import settle_margin_from_env\n'
        'settle_margin_from_env({}, 2)\n'
        'settle_margin_from_env({}, 2)  # type: ignore[call-arg]\n'
    )
    checked, findings, _ = scan_source(source, 'probe.py')
    assert (checked, [f.problem for f in findings]) == (1, ['2 positional arguments, accepts 1'])


def test_a_plain_dataclass_constructor_is_judged():
    source = 'from data.store.app.health import DatasetHealth\nDatasetHealth(gap_count=1)\nDatasetHealth(s, staus=1)\n'
    _, findings, _ = scan_source(source, 'probe.py')
    assert [f.problem for f in findings] == ["missing required 'status'", "unknown keyword 'staus'"]
