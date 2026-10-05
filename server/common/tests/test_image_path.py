"""The host-side half of common/tests/image_path.py: what /code resolves to on THIS side of the move.

The container-relative half -- IMAGE_PYTHONPATH_ENTRIES against every ENV PYTHONPATH in the
Dockerfile -- is pinned in test_ci_invariants.py and is unaffected by tj-iontkq.4, because /code did
not move. What DID change, and what nothing asserted, is the mapping from that one image directory
onto the TWO checkout directories it is now assembled from: the Dockerfile COPYs the four service
trees out of ./server, while pytest.ini, gen/proto/python and the bind-mounted tests/ stay at the
repository root. image_import_roots grew a second parameter for exactly that, and two properties of
the result are load-bearing with no assertion behind either.

  BOTH ROOTS ARE YIELDED for the code-root entry. Every synthetic caller depends on it:
  test_ci_invariants' _first_party_module_file monkeypatches CHECKOUT_CODE_ROOT to a tmp_path tree
  that is FLAT -- no server/ inside it -- and finds its modules only because context_root is
  searched alongside a root that does not exist. Drop either from the pair and those searches go
  quietly empty, which in a first-party-import guard means it stops finding anything to object to.
  That is a passing test that checks nothing, not a failing one.

  THE PAIR COLLAPSES WHEN THE TWO ARE THE SAME DIRECTORY, so a single-rooted caller gets the
  two-entry list this function always returned rather than a repeated entry. A duplicated
  PYTHONPATH entry is harmless to an interpreter, which is precisely why nothing downstream would
  ever go red over it.

  AND THE ORDER IS THE IMAGE'S. The code root comes before gen/proto/python, and the server root
  before the repository root, because that is the order an interpreter searches: `import common`
  must reach the real package under server/ and not whatever sits at the repository root under the
  same name (bug tj-fts1lo).
"""

import os
from pathlib import Path

import pytest

from common.tests.image_path import (
    IMAGE_CODE_ROOT_ENTRY,
    IMAGE_PYTHONPATH_ENTRIES,
    image_import_roots,
    image_pythonpath,
)
from common.tests.roots import REPO_ROOT, SERVER_ROOT


pytestmark = pytest.mark.common

GENERATED = Path('gen/proto/python')


def test_the_default_roots_are_the_two_sentinels_in_the_images_order():
    """THE PIN. Server root first -- the import root -- then the repository root, then the gen tree."""
    assert image_import_roots() == [SERVER_ROOT, REPO_ROOT, REPO_ROOT / GENERATED]


def test_the_code_root_entry_yields_both_roots(tmp_path: Path):
    """Both, always: one image directory is two checkout directories since the trees moved.

    The synthetic callers monkeypatch a flat tree and pass it as the context root while the server
    root points at a `server/` that is not there. They find their modules through the second element
    of this pair, so losing it empties their search with nothing going red.
    """
    server, context = tmp_path / 'server', tmp_path
    assert image_import_roots(server, context)[:2] == [server, context]


def test_the_two_roots_collapse_to_one_entry_when_they_are_the_same_directory(tmp_path: Path):
    """The pre-move and single-rooted-synthetic shape: two entries, not a repeated one."""
    assert image_import_roots(tmp_path, tmp_path) == [tmp_path, tmp_path / GENERATED]


def test_every_entry_after_the_code_root_resolves_against_the_context_root(tmp_path: Path):
    """gen/proto/python stays at the top of the repository; only the service trees moved."""
    server, context = tmp_path / 'server', tmp_path
    trailing = [entry for entry in IMAGE_PYTHONPATH_ENTRIES if entry != IMAGE_CODE_ROOT_ENTRY]
    assert image_import_roots(server, context)[2:] == [context / str(entry) for entry in trailing]


def test_the_pythonpath_is_the_roots_joined_by_the_path_separator(tmp_path: Path):
    """image_pythonpath adds nothing of its own; it is image_import_roots, spelled for an env var."""
    server, context = tmp_path / 'server', tmp_path
    assert image_pythonpath(server, context) == os.pathsep.join(
        str(root) for root in image_import_roots(server, context)
    )
    assert image_pythonpath() == os.pathsep.join(str(root) for root in image_import_roots())


def test_the_default_server_root_is_the_one_that_actually_holds_the_service_trees():
    """So a subprocess handed this path imports the real package rather than failing to.

    The first entry is what `import common` resolves against. REPO_ROOT is not, and saying so costs
    one assertion -- image_path.py's docstring has said it in prose since tj-iontkq.2.
    """
    first, *_ = image_import_roots()
    assert (first / 'common' / '__init__.py').is_file()
    assert (REPO_ROOT / GENERATED).is_dir()
