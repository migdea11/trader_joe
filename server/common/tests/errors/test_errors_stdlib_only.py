"""common/errors is pure Python: the standard library and nothing else (TE-1 tj-3mk3u5.37.3; ADR tj-fa1rpu U1).

The bead: no grpc, no FastAPI, no transport. The vocabulary is canonical in Python and rendered elsewhere,
and TE-8's docs/errors.md generator is a stdlib-only script that imports it. Two checks, because each misses
what the other sees. Importing the module in a fresh interpreter catches anything loaded transitively,
the parent package included. Reading the source catches an import inside a function body, which importing
the module never runs.
"""

import ast
import importlib.util
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import common.errors
from common.tests.roots import SERVER_ROOT


pytestmark = pytest.mark.common

# THE SERVER ROOT (tj-iontkq.2). The root is used to turn a source path into a dotted module name
# (common.errors.vocabulary) and as the cwd of a fresh interpreter that imports common.errors, so it
# is the IMPORT root. The PACKAGE_DIR.parents[1] it replaces tracked the move by accident, being
# derived from the imported module rather than from this file -- which is precisely the
# accidentally-correct index the named sentinel exists to stop someone "fixing".
PACKAGE = common.errors.__name__
PACKAGE_DIR = Path(common.errors.__file__).resolve().parent
SOURCES = sorted(PACKAGE_DIR.rglob('*.py'))

# Run in a fresh interpreter: print every module the import adds whose top-level package is not stdlib.
PROBE = '\n'.join(
    [
        'import sys',
        'before = set(sys.modules)',
        'import common.errors.vocabulary',
        'added = set(sys.modules) - before',
        "print(*sorted(m for m in added if m.partition('.')[0] not in sys.stdlib_module_names), sep='\\n')",
    ]
)


def _in_package(name: str) -> bool:
    return name == PACKAGE or name.startswith(f'{PACKAGE}.')


def _imports(path: Path) -> Iterator[tuple[int, str]]:
    """Yield (line, absolute module name) for every import statement in path, at any depth."""
    package = '.'.join(path.relative_to(SERVER_ROOT).with_suffix('').parts[:-1])
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'), filename=str(path))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                yield node.lineno, importlib.util.resolve_name('.' * node.level + (node.module or ''), package)
            else:
                yield node.lineno, str(node.module)


def test_the_package_has_source_to_check():
    """Guards the parametrised test below against passing over an empty list."""
    assert PACKAGE_DIR / 'vocabulary.py' in SOURCES


def test_importing_the_vocabulary_loads_nothing_outside_the_standard_library():
    """A fresh interpreter that imports the vocabulary loads only the stdlib and the package's own modules."""
    result = subprocess.run(
        [sys.executable, '-c', PROBE], cwd=SERVER_ROOT, capture_output=True, text=True, check=False, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert set(result.stdout.split()) == {'common', PACKAGE, f'{PACKAGE}.vocabulary'}


@pytest.mark.parametrize('path', SOURCES, ids=lambda path: str(path.relative_to(SERVER_ROOT)))
def test_every_import_in_the_package_is_stdlib_or_the_package_itself(path: Path):
    """No module under common/errors imports anything but the stdlib or common.errors, at any depth."""
    outside = [
        f'line {line}: {name}'
        for line, name in _imports(path)
        if name.partition('.')[0] not in sys.stdlib_module_names and not _in_package(name)
    ]
    assert not outside, f'{path.name} imports outside the standard library: {outside}'
