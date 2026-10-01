"""No production module references the test tree (decision tj-j4wknb R4).

R4: production code carries no test instrumentation. Fakes live under tests/ and reach a running
service only through the test-only launcher (tests/fakes/ingest_launcher.py) and a compose
overlay, never because production imports them. The prod image copies common, routers, schemas
and <service>/app and nothing under tests/, so an import of the test tree from production is
either a crash in prod or -- worse, if someone "fixes" the Dockerfile -- fakes shipped in prod.

THE SCAN IS STATIC, over the source, not over sys.modules: an import that only runs on some path
(inside a function, under TYPE_CHECKING, behind a flag) is exactly the kind a runtime check
misses, and parsing finds it wherever it sits.

SCOPE: every module under the roots below -- data_ingest's own app and routers, plus common and
schemas, which the data_ingest image also carries. Test directories inside those roots are
skipped; they are the test tree.

MARKER: data_ingest. Under pytest.ini's rule a test carries the marker of the component whose
interface it drives; this one drives none, so the marker names the component whose production
image it guards -- the roots scanned are exactly what the data_ingest image is built from.
"""

import ast
from pathlib import Path

import pytest


pytestmark = pytest.mark.data_ingest

REPO_ROOT = Path(__file__).resolve().parents[3]
PRODUCTION_ROOTS = ('data/ingest/app', 'routers/data_ingest', 'common', 'schemas')
TEST_PACKAGE = 'tests'


def production_modules() -> list[Path]:
    """Every Python source file under the production roots, test directories excluded.

    Returns:
        list[Path]: Source files, repo-relative order.
    """
    return sorted(
        path
        for root in PRODUCTION_ROOTS
        for path in (REPO_ROOT / root).rglob('*.py')
        if TEST_PACKAGE not in path.relative_to(REPO_ROOT).parts
    )


def package_of(path: Path) -> list[str]:
    """The dotted package a module sits in, as the parts a relative import resolves against."""
    return list(path.relative_to(REPO_ROOT).parent.parts)


def imported_modules(path: Path) -> list[tuple[int, str]]:
    """Every module a source file imports, relative imports resolved to absolute names.

    Args:
        path (Path): Source file.

    Returns:
        list[tuple[int, str]]: (line, absolute dotted module name) per import.
    """
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                package = package_of(path)
                base = package[: len(package) - (node.level - 1)]
                module = '.'.join([*base, *([node.module] if node.module else [])])
            else:
                module = node.module or ''
            found.append((node.lineno, module))
            # `from . import tests` names the package in the alias, not in node.module.
            found.extend((node.lineno, f'{module}.{alias.name}') for alias in node.names)
    return found


def is_test_tree(module: str) -> bool:
    """Whether a dotted module name is, or lies inside, a test package: tests.*, or any *.tests.*."""
    return TEST_PACKAGE in module.split('.')


def test_the_scan_reads_every_production_root():
    # A root that moved or emptied would make the scan below pass having read nothing.
    for root in PRODUCTION_ROOTS:
        assert any(path.is_relative_to(REPO_ROOT / root) for path in production_modules()), f'{root} has no modules'


def test_the_scan_recognises_an_import_of_the_test_tree():
    # The detector itself, against each import form it must catch -- so a scan that silently
    # stopped matching could not keep the test below green.
    assert is_test_tree('tests')
    assert is_test_tree('tests.fakes.market_data')
    assert is_test_tree('data.ingest.tests.test_read_seam')
    assert not is_test_tree('data.ingest.app.brokers.interface')
    assert not is_test_tree('testsuite')


def test_no_production_module_imports_the_test_tree():
    offenders = [
        f'{path.relative_to(REPO_ROOT)}:{line} imports {module}'
        for path in production_modules()
        for line, module in imported_modules(path)
        if is_test_tree(module)
    ]

    assert not offenders, 'production code imports the test tree (decision tj-j4wknb R4):\n' + '\n'.join(offenders)
