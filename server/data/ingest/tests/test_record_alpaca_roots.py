"""The recorder's own derivation of the server root -- the fourth spelling, and the silent one.

tests/fakes/record_alpaca.py derives SERVER_ROOT locally; its comment gives the reason, and it is
the strongest of the four: the file is run as a SCRIPT, so sys.path[0] is tests/fakes and `common`
is not importable from it at all. common/tests/test_roots.py's inventory holds that derivation to
the canonical one in today's checkout. This file holds it to the behaviour the inventory cannot
see -- what it does in a checkout the inventory never runs in.

WHY IT NEEDED ITS OWN TESTS AT ALL (bug tj-fts1lo, architect gate). The fix for tj-fts1lo landed at
two of four derivation sites. This one was missed, and it has the collapsing site's exact shape in
the collapsing site's exact direction: DOWNWARD from the repository root, REPO ROOT TRIED FIRST, so
the stale untracked shells a pre-move checkout keeps at the old tree paths beat the real server/
below them. Measured against the shared checkout with those shells present, the old `is_dir()`
predicate returned the repository root (COLLAPSED) while the package-keyed one returned server/.

AND ITS COLLAPSE IS SILENT, WHICH IS WHY NOTHING WENT RED. The recorder does not read DEFAULT_OUT
before writing to it -- the write is `out.mkdir(parents=True, exist_ok=True)`. So a developer
re-recording Alpaca fixtures in a pre-move checkout CREATES data/ingest/tests/fixtures/alpaca at
the repository root, writes a credentialed sitting's fixtures into it, prints success, and leaves
the suite reading the stale committed fixtures under server/. Nothing fails, nothing changes, and
the sitting is spent. The recorder's own comment states the standard that fails: "writing recorded
fixtures to a guessed directory is worse than not writing them."

NOTHING ELSE PINS DEFAULT_OUT. The suite's only other reference to it,
test_record_alpaca.py's `recorder` fixture, monkeypatches it to tmp_path -- correctly, since no
test may write into the committed fixtures -- so the real value was asserted nowhere.

These tests live here rather than beside the script because tests/fakes may hold no test module;
test_record_alpaca.py gives that reason and the recorder's other tests are already here.
"""

from pathlib import Path

import pytest

from common.tests.roots import REPO_ROOT, SERVER_ROOT
from tests.fakes.record_alpaca import DEFAULT_OUT, PACKAGE_MARKER, SERVER_MARKERS, _server_root


pytestmark = pytest.mark.data_ingest

# The ruled layout: the four service trees live under server/, a child of the true repository root.
SERVER_DIR_NAME = 'server'
# Where the recorder writes, relative to whichever root it derives.
FIXTURES_UNDER_SERVER_ROOT = ('data', 'ingest', 'tests', 'fixtures', 'alpaca')


def _shell(parent: Path, name: str) -> None:
    """A stale leftover: the right directory name holding only untracked bytecode."""
    cache = parent / name / '__pycache__'
    cache.mkdir(parents=True)
    (cache / f'{name}.cpython-312.pyc').write_bytes(b'\x00')


def _package(parent: Path, name: str) -> None:
    """A real tree: the right directory name, carrying __init__.py."""
    (parent / name).mkdir(parents=True)
    (parent / name / PACKAGE_MARKER).write_text('', encoding='utf-8')


def _checkout(root: Path) -> Path:
    """A synthetic checkout: pytest.ini at ROOT, and the recorder sitting at tests/fakes under it."""
    (root / 'pytest.ini').write_text('[pytest]\n', encoding='utf-8')
    start = root / 'tests' / 'fakes'
    start.mkdir(parents=True)
    return start


# --- the live checkout ---------------------------------------------------------------------------


def test_the_recorders_server_root_is_the_one_the_canonical_module_derives():
    """THE MISSING PIN, from this side: the fourth spelling answers to the first."""
    assert _server_root() == SERVER_ROOT


def test_the_recorders_server_root_has_not_collapsed_onto_the_repository_root():
    """Stated separately because collapse is the specific failure, not inequality in general."""
    assert _server_root() != REPO_ROOT
    assert _server_root() == REPO_ROOT / SERVER_DIR_NAME


def test_the_recorder_writes_under_the_server_root_and_not_under_the_repository_root():
    """DEFAULT_OUT itself, which nothing asserted: every other reference monkeypatches it away."""
    under_the_server_root = SERVER_ROOT.joinpath(*FIXTURES_UNDER_SERVER_ROOT)
    under_the_repository_root = REPO_ROOT.joinpath(*FIXTURES_UNDER_SERVER_ROOT)
    # Written root-first because ruff reads an upper-case name on the left as a Yoda condition
    # (SIM300); DEFAULT_OUT is the subject of both assertions either way.
    assert under_the_server_root == DEFAULT_OUT
    assert under_the_repository_root != DEFAULT_OUT


def test_the_directory_the_recorder_would_write_into_is_the_one_the_suite_reads():
    """The whole point of the derivation, and the half mkdir(exist_ok=True) can never report.

    A collapsed root names a directory that does not exist yet and the recorder creates it, so
    "the path resolves" is not evidence. The committed fixtures being THERE is.
    """
    assert DEFAULT_OUT.is_dir(), DEFAULT_OUT
    assert sorted(path.name for path in DEFAULT_OUT.glob('*.json')), f'no committed fixtures in {DEFAULT_OUT}'


# --- the marker discriminates ----------------------------------------------------------------------


def test_stale_shells_at_the_repository_root_do_not_win_over_the_real_server_tree(tmp_path: Path):
    """THE REGRESSION, in the shape this site actually has: downward, repository root tried FIRST.

    Untracked leftovers at the old tree paths, the genuine packages under server/. Under the old
    `is_dir()` marker this returned tmp_path -- the collapse, and here a silent one. It must
    return tmp_path/server.
    """
    start = _checkout(tmp_path)
    for name in SERVER_MARKERS:
        _shell(tmp_path, name)
        _package(tmp_path / SERVER_DIR_NAME, name)
    assert _server_root(start) == tmp_path / SERVER_DIR_NAME


def test_a_repository_root_that_holds_the_packages_itself_is_still_the_server_root(tmp_path: Path):
    """The pre-move layout, and the branch the fix must not break: the root itself can qualify."""
    start = _checkout(tmp_path)
    for name in SERVER_MARKERS:
        _package(tmp_path, name)
    assert _server_root(start) == tmp_path


def test_a_child_holding_only_some_of_the_packages_is_not_the_server_root(tmp_path: Path):
    """All three, so a half-finished move does not quietly nominate a root."""
    start = _checkout(tmp_path)
    for name in SERVER_MARKERS[:-1]:
        _package(tmp_path / SERVER_DIR_NAME, name)
    _shell(tmp_path / SERVER_DIR_NAME, SERVER_MARKERS[-1])
    with pytest.raises(RuntimeError, match='packages'):
        _server_root(start)


def test_the_search_refuses_rather_than_falling_back(tmp_path: Path):
    """No fallback: writing recorded fixtures to a guessed directory is worse than not writing them."""
    start = _checkout(tmp_path)
    with pytest.raises(RuntimeError, match='packages'):
        _server_root(start)


def test_a_tree_with_no_pytest_ini_above_it_refuses_too(tmp_path: Path):
    """The other search in the same function. It raises rather than naming the filesystem root."""
    start = tmp_path / 'tests' / 'fakes'
    start.mkdir(parents=True)
    with pytest.raises(RuntimeError, match=r'pytest\.ini'):
        _server_root(start)
