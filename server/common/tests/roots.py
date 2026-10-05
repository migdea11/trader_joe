"""The two roots this suite measures from, found by MARKER rather than by counting parent directories.

WHY TWO NAMES. Until this module existed, every test that needed a root wrote
`Path(__file__).resolve().parents[N]` and called the result REPO_ROOT. That single name hid the fact
that two DIFFERENT roots were being asked for, which happen to be the same directory in today's
layout and stop being the same the moment the service trees move down a level (epic tj-iontkq, risk
R-1):

  REPO_ROOT    The TRUE repository root. What reads Makefile, pytest.ini, Dockerfile, the compose
               files, .env.default, .dockerignore, .github/workflows, .devcontainer, .claude,
               pyproject.toml, uv.lock, gen/ and tools/ -- every one of which stays at the top of
               the repository.
  SERVER_ROOT  The directory the service trees live in: the parent of common/, routers/, schemas/
               and data/. What is used for relative_to() to produce a dotted module name or a
               tree-relative source path, and for the PYTHONPATH= and cwd= of a subprocess that
               imports a service module -- in other words, the import root, which is what the
               service image calls /code.

A COUNTED INDEX CANNOT TELL THOSE APART, and that is the whole hazard. A site that wants the true
repository root and silently receives the server root does not get an ImportError -- it gets a
directory where Makefile, pytest.ini and the compose files simply are not, so the files it reads go
missing and some of its assertions pass VACUOUSLY. That is this project's recorded failure shape
(tj-0qxnzw, tj-06uflo): a green result that asserted nothing. A marker search cannot drift that way,
because the marker IS the definition of the root rather than a distance from one file to it.

NEITHER SEARCH MAY FALL BACK. Each raises when no ancestor carries its marker. A sentinel that
quietly returned the filesystem root would be the same vacuous-green failure in a new place: every
path built from it would exist nowhere, and a test asking `is_dir()` on it would simply find nothing.

Stdlib only, and no test functions: common/tests, common/tests/errors, data/*/tests, routers/tests,
schemas/tests and tests/system all import it, the last inside the test_client container.
"""

from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath


# The true repository root is the directory holding pytest's own configuration. pytest.ini is the
# marker rather than .git because it is also what defines the rootdir every test path is reported
# against, and because a checkout exported without .git must still resolve.
REPO_MARKER = 'pytest.ini'

# The server root is the directory holding the three trees every service imports from. ALL THREE
# must be present and must be directories, so no single stray file or unrelated directory of the
# same name can satisfy the search. data/ is deliberately not in the set: it is a service tree
# rather than a shared one, and requiring it would make this module unusable from a checkout that
# carried only the shared library.
SERVER_MARKERS = ('common', 'routers', 'schemas')

# Where both searches start: this file's own directory. Both roots are ancestors of it in every
# layout -- common/tests today, server/common/tests after the move -- so the result follows the
# file instead of being asserted about it.
_START = Path(__file__).resolve().parent


def _nearest_ancestor(start: Path, carries_marker: Callable[[Path], bool], marker: str) -> Path:
    """The nearest of START and its ancestors for which CARRIES_MARKER holds, or raise.

    Raising is the point: see NEITHER SEARCH MAY FALL BACK above. There is no default and no
    filesystem-root fallback, so a caller either gets a directory that really carries the marker
    or gets an exception at import time.
    """
    for candidate in (start, *start.parents):
        if carries_marker(candidate):
            return candidate
    raise RuntimeError(
        f'no ancestor of {start} carries {marker}, so the root it marks cannot be located. '
        f'Searched {start} and its {len(start.parents)} parents up to {start.anchor!r}.'
    )


def find_repo_root(start: Path = _START) -> Path:
    """The nearest ancestor of START containing pytest.ini -- the TRUE repository root."""
    return _nearest_ancestor(start, lambda candidate: (candidate / REPO_MARKER).is_file(), REPO_MARKER)


def find_server_root(start: Path = _START, markers: Iterable[str] = SERVER_MARKERS) -> Path:
    """The nearest ancestor of START containing every name in MARKERS as a directory."""
    names = tuple(markers)
    return _nearest_ancestor(
        start, lambda candidate: all((candidate / name).is_dir() for name in names), f'all of {names} as directories'
    )


REPO_ROOT = find_repo_root()
SERVER_ROOT = find_server_root()


# -------------------------------------------------------------------------------------------------
# SITES THAT NAME A WHOLE LIST OF TREES, where the list spans BOTH roots.
#
# Several guards do not ask for one root -- they carry a tuple of repository-relative tree names and
# walk every one of them: test_declared_imports' SCANNED_ROOTS, both test_no_production_test_imports'
# PRODUCTION_ROOTS, test_latency_rest_client's SOURCE_TREES. Those tuples mix the two roots in a
# single literal -- common/, routers/, schemas/ and data/ belong to the server root, while
# gen/proto/python, tests/ and tools/ stay at the top of the repository -- so NEITHER sentinel alone
# is the right answer for such a site, and picking one would be the guess this module exists to
# remove. The two functions below take the name and hand back the root that OWNS it, which keeps the
# tuples themselves as repository-relative literals and correct across the move with no further edit.
#
# Today both roots are the same directory, so both functions are exactly equivalent to the
# `REPO_ROOT / name` and `path.relative_to(REPO_ROOT)` they replace. That equivalence is what makes
# this a pure refactor now and the right answer later.

# The trees that travel with the services. A repository-relative name whose FIRST component is one
# of these resolves under SERVER_ROOT; every other name resolves under REPO_ROOT.
SERVER_TREES = ('common', 'routers', 'schemas', 'data')


def tree_root(name: str) -> Path:
    """The root that owns the repository-relative tree name NAME -- SERVER_ROOT or REPO_ROOT."""
    first = PurePosixPath(name).parts[0]
    return SERVER_ROOT if first in SERVER_TREES else REPO_ROOT


def resolve_tree(name: str) -> Path:
    """The absolute path of the repository-relative tree name NAME, under whichever root owns it."""
    return tree_root(name) / name


def repo_relative(path: Path) -> Path:
    """PATH as the repository names it: `common/rpc/ping.py`, `tests/fakes/market_data.py`.

    These names are compared against committed data -- the route manifests, the Dockerfile's COPY
    sources, dotted module names -- so they must stay server-relative for a server tree and
    repository-relative for everything else. SERVER_ROOT is tried first because a server file sits
    under both roots once the two diverge, and it is the shorter, committed spelling that wins.
    """
    resolved = Path(path).resolve()
    if resolved.is_relative_to(SERVER_ROOT):
        return resolved.relative_to(SERVER_ROOT)
    return resolved.relative_to(REPO_ROOT)
