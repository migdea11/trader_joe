"""The two sentinels in common/tests/roots.py, and the one fact nothing pinned: THEY ARE DISTINCT.

Bug tj-fts1lo. Epic tj-iontkq spent four tasks separating REPO_ROOT from SERVER_ROOT, and when the
service trees finally moved under server/ nothing asserted that the separation had actually taken
effect. It had not, at another derivation of the same pair -- tools/agent_mcp/tests/harness.py --
and the collapse was found by hand rather than by this suite. A collapsed pair restores the
PRE-MOVE value, so every caller that merely needs *a* root keeps passing; only a caller naming a
file that genuinely moved goes red. That is the vacuous-green shape this project keeps finding, so
the distinctness itself is now an assertion rather than an implication of one.

THE PAIR IS DERIVED IN FOUR PLACES, NOT TWO, and this file's first version said two. That sentence
was wrong in the direction that costs the most: it read as though every spelling had been checked,
while tests/fakes/record_alpaca.py was collapsing in the shared checkout and nothing looked. The
four are this module, tools/agent_mcp/tests/harness.py, tests/fakes/record_alpaca.py and
server/data/store/run_migrations.sh, and all four are now held to this one by
test_every_derivation_of_the_two_roots_agrees -- see THE INVENTORY below for why they may not be
collapsed into a single spelling.

WHY THE MARKER IS A PACKAGE AND NOT A DIRECTORY. `git mv` relocates tracked files only, so a
checkout that predates the move keeps common/, routers/ and schemas/ at the REPOSITORY root holding
nothing but untracked __pycache__. Those shells answer `is_dir()`. They are untracked, so CI --
which builds fresh -- never sees them and nothing in version control can delete them: the marker
has to be what tells them apart, and none of them carries an __init__.py.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from common.tests.roots import (
    PACKAGE_MARKER,
    REPO_MARKER,
    REPO_ROOT,
    SERVER_MARKERS,
    SERVER_ROOT,
    find_repo_root,
    find_server_root,
    is_package,
)


pytestmark = pytest.mark.common

# The ruled layout: the four service trees live under server/, a child of the true repository root.
SERVER_DIR_NAME = 'server'


def _shell(parent: Path, name: str) -> Path:
    """A stale leftover: a directory of the right name holding only untracked bytecode."""
    cache = parent / name / '__pycache__'
    cache.mkdir(parents=True)
    (cache / f'{name}.cpython-312.pyc').write_bytes(b'\x00')
    return parent / name


def _package(parent: Path, name: str) -> Path:
    """A real tree: a directory of the right name that is an importable package."""
    (parent / name).mkdir(parents=True)
    (parent / name / PACKAGE_MARKER).write_text('', encoding='utf-8')
    return parent / name


def _checkout_with_stale_shells(root: Path) -> Path:
    """A repository root carrying the three STALE shells, with the real packages under server/.

    This is the condition tj-fts1lo was reported against, reproduced exactly: untracked leftovers at
    the old tree paths and the genuine trees one level down.
    """
    (root / REPO_MARKER).write_text('[pytest]\n', encoding='utf-8')
    for name in SERVER_MARKERS:
        _shell(root, name)
        _package(root / SERVER_DIR_NAME, name)
    return root / SERVER_DIR_NAME


# --- the live checkout -------------------------------------------------------------------------


def test_the_two_roots_are_distinct_in_this_checkout():
    """THE MISSING PIN. Once server/ exists the two sentinels are two different directories."""
    assert SERVER_ROOT != REPO_ROOT


def test_the_server_root_is_the_server_directory_under_the_repository_root():
    """And they are distinct in the one specific way the ruled layout says, not just unequal."""
    assert SERVER_ROOT == REPO_ROOT / SERVER_DIR_NAME


def test_the_repository_root_is_the_one_holding_pytest_ini_and_the_server_root_holds_the_packages():
    """Each sentinel carries its own marker, so neither is merely the other plus a guess."""
    assert (REPO_ROOT / REPO_MARKER).is_file()
    assert all(is_package(SERVER_ROOT, name) for name in SERVER_MARKERS)


def test_the_shared_packages_import_from_under_the_server_root():
    """The other half of the same hazard, and it is reachable through sys.path rather than a marker.

    pytest.ini's `pythonpath = .` puts the TRUE REPOSITORY ROOT on sys.path -- it has to, or tests/
    and tools/ stop being importable, which is the correction tj-iontkq.4 made to the epic's X-1
    (thirteen modules stopped collecting without it). That is also exactly where the stale shells
    live. What keeps `import common` reaching the real tree is Python's rule that a REGULAR package
    anywhere on the path beats a namespace portion: server/common carries __init__.py and the shell
    does not. The rule holds whatever the sys.path order, so this is a pin and not a worry -- but it
    is load-bearing and nothing else states it.

    FOUR NAMES, NOT THREE. The marker set is three, deliberately (see roots.py's comment: data/ is a
    service tree rather than a shared one). The sys.path hazard is not three: `git mv` stranded FOUR
    shells at the repository root -- common, routers, schemas AND data, every one of them holding
    only untracked __pycache__ -- and `data` is a top-level import name in SERVER_TREES and in
    ruff's known-first-party. The regular-package-beats-namespace-portion argument covers it
    unchanged; it was simply not being stated, which is how the gap in the marker set got read as a
    gap in this pin.
    """
    import common
    import data
    import routers
    import schemas

    for package in (common, data, routers, schemas):
        assert Path(package.__file__).resolve().parent.parent == SERVER_ROOT, package.__name__


# --- every derivation of the pair, held to this one ----------------------------------------------
#
# THE INVENTORY, and why it is a list rather than a second cross-check (tj-fts1lo, architect gate).
# The first version of this test pinned roots.py against harness.py and said the definition was
# "spelled twice". It is spelled FOUR times, and a cross-check that certifies two of four while
# reading as though it certified all of them is worse than no cross-check: it is the statement that
# stops the next reader looking. It did -- tests/fakes/record_alpaca.py was collapsing in the shared
# checkout the whole time that sentence was true of the two sites that were not.
#
# THE DUPLICATION STAYS; THE CONTROL IS CONFORMANCE, NOT DE-DUPLICATION. Each extra spelling is
# forced by where it sits, not chosen:
#   - harness.py cannot import this module. pytest.ini sets `pythonpath = . gen/proto/python` and
#     nothing more, so server/ reaches sys.path only through prepend import mode from a test module
#     collected UNDER server/. A tools-scoped run (make test PATHS=tools) collects no such module,
#     so `import common.tests.roots` would be an ImportError there while a whole-tree run stayed
#     green -- the worst available failure mode.
#   - record_alpaca.py cannot either, and for a stronger reason: it is run as a SCRIPT, so
#     sys.path[0] is tests/fakes and `common` is not importable at all.
#   - run_migrations.sh is shell.
# And the inverse -- hoisting a canonical definition to a repository-root module -- breaks this
# module's own contract: tests/system imports it INSIDE the test_client container, which mounts the
# server trees and pytest.ini, not a root module. Locating this file by path from the repository
# root is rejected outright: that downward guess is the bug.
# So a new spelling may exist. It must be ANSWERABLE TO THIS ONE BY A TEST, and that is this list.

# How each site names the pair. A site that derives only one of the two roots reports None for the
# other rather than having a name invented for it.
SHELL_SITE = 'server/data/store/run_migrations.sh'
MIGRATIONS_UNDER_SERVER_ROOT = ('data', 'store', 'migrations', 'versions')
# What run_migrations.sh prints before it reaches docker, and the only line that names a root.
MIGRATION_DIR_LINE = re.compile(r'^Running .* against the revisions in (?P<dir>.+)$', re.MULTILINE)


def _roots_from_the_shell_helper(tmp_path: Path) -> tuple[Path, Path]:
    """(REPO_ROOT, SERVER_ROOT) as run_migrations.sh derives them, read off a real invocation.

    A SUBPROCESS WITH `docker` STUBBED is how this suite already reaches shell helpers
    (test_service_pythonpath.py, test_grpc_peer_reach.py), and it is what lets the shell spelling
    join the inventory without editing a file outside the validator's scope. `upgrade head` and not
    `check`: check runs the comparison-image guard, which wants a real image. The script prints
    MIGRATION_DIR -- SERVER_ROOT plus a fixed tail -- then `cd`s to REPO_ROOT and calls
    `docker compose ps -q postgres`, so the stub's own $PWD IS the repository root it derived. The
    stub prints nothing, so the script then exits 1 on "postgres is not running", which is after
    both roots have been observed and is therefore the expected status.
    """
    bash = shutil.which('bash')
    assert bash, 'bash is not on PATH, so the shell derivation cannot be exercised'
    log = tmp_path / 'docker-cwd.log'
    stub = tmp_path / 'bin' / 'docker'
    stub.parent.mkdir(parents=True)
    stub.write_text(f'#! /bin/bash\nprintf "%s\\n" "$PWD" >> {log}\nexit 0\n', encoding='utf-8')
    stub.chmod(0o755)

    result = subprocess.run(
        [bash, str(REPO_ROOT / SHELL_SITE), 'upgrade', 'head'],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, 'PATH': f'{stub.parent}{os.pathsep}{os.environ["PATH"]}'},
    )
    printed = MIGRATION_DIR_LINE.search(result.stdout)
    assert printed, f'{SHELL_SITE} named no migration directory.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}'
    assert log.is_file(), (
        f'{SHELL_SITE} never reached docker, so it named no repository root.\nstderr:\n{result.stderr}'
    )

    migration_dir = Path(printed.group('dir'))
    server_root = migration_dir
    for _ in MIGRATIONS_UNDER_SERVER_ROOT:
        server_root = server_root.parent
    return Path(log.read_text(encoding='utf-8').splitlines()[0]), server_root


def test_every_derivation_of_the_two_roots_agrees(tmp_path: Path):
    """ALL FOUR SPELLINGS, held to this module's pair. See THE INVENTORY above for why four.

    Add a derivation and it belongs here. The entries are (site, repo root or None, server root):
    None where a site genuinely derives only one of the two, so no name is invented for it.
    """
    from tests.fakes import record_alpaca
    from tools.agent_mcp.tests import harness

    shell_repo_root, shell_server_root = _roots_from_the_shell_helper(tmp_path)
    inventory = (
        ('common/tests/roots.py', REPO_ROOT, SERVER_ROOT),
        ('tools/agent_mcp/tests/harness.py', harness.REPO_ROOT, harness.SERVER_ROOT),
        ('tests/fakes/record_alpaca.py', None, record_alpaca._server_root()),
        (SHELL_SITE, shell_repo_root, shell_server_root),
    )

    disagreed = [
        f'{site}: derived ({repo}, {server}), this module derives ({REPO_ROOT}, {SERVER_ROOT})'
        for site, repo, server in inventory
        if (repo not in (None, REPO_ROOT)) or server != SERVER_ROOT
    ]
    assert not disagreed, 'derivations of the two roots disagree:\n' + '\n'.join(disagreed)
    assert len(inventory) == 4, 'the inventory must name every derivation in the repository'


# --- the marker discriminates ------------------------------------------------------------------


def test_a_stale_shell_tree_cannot_name_a_server_root(tmp_path: Path):
    """THE REGRESSION. Directories of the right name holding only __pycache__ satisfy nothing.

    Started from inside the stale copy, the old `is_dir()` predicate matched its parent and handed
    back the repository root -- the collapse. The package marker leaves nothing to match, and the
    search refuses rather than falling back, so the failure is loud wherever it does occur.
    """
    _checkout_with_stale_shells(tmp_path)
    with pytest.raises(RuntimeError, match='packages'):
        find_server_root(start=tmp_path / 'common' / 'tests')


def test_the_server_root_is_found_through_the_shells_from_inside_the_real_tree(tmp_path: Path):
    """And the real tree still resolves, with the shells sitting one level above it."""
    server = _checkout_with_stale_shells(tmp_path)
    assert find_server_root(start=server / 'common' / 'tests') == server


def test_every_marker_must_be_a_package_not_just_most_of_them(tmp_path: Path):
    """All three, so a checkout that moved two trees and not the third is not a server root."""
    for name in SERVER_MARKERS[:-1]:
        _package(tmp_path, name)
    _shell(tmp_path, SERVER_MARKERS[-1])
    with pytest.raises(RuntimeError, match='packages'):
        find_server_root(start=tmp_path)


def test_a_namespace_directory_is_not_a_package(tmp_path: Path):
    """A directory with real content but no __init__.py imports as a namespace package, not ours.

    The shells are the untracked case; this is the tracked one. Either way the server root is where
    the PACKAGES live, and `is_package` is the whole of that definition.
    """
    for name in SERVER_MARKERS:
        (tmp_path / name).mkdir()
        (tmp_path / name / 'module.py').write_text('', encoding='utf-8')
    assert not any(is_package(tmp_path, name) for name in SERVER_MARKERS)


# --- the repository marker is not vulnerable to the same shape -----------------------------------


def test_the_repository_marker_is_a_file_so_a_directory_of_that_name_cannot_satisfy_it(tmp_path: Path):
    """Confirmed rather than assumed (tj-fts1lo).

    REPO_MARKER is keyed on `is_file()`, and the leftovers a rename strands are DIRECTORIES. So the
    shape that collapsed the server root cannot reach the repository root: a directory named
    pytest.ini is not a repository root, and pytest.ini has never lived anywhere but the top of the
    repository for a rename to strand a copy of it lower down.
    """
    (tmp_path / REPO_MARKER).mkdir()
    with pytest.raises(RuntimeError, match=REPO_MARKER):
        find_repo_root(start=tmp_path)

    real = tmp_path / 'checkout'
    real.mkdir()
    (real / REPO_MARKER).write_text('[pytest]\n', encoding='utf-8')
    assert find_repo_root(start=real / 'server' / 'common') == real


def test_neither_search_falls_back_to_the_filesystem_root(tmp_path: Path):
    """Both refuse when nothing carries the marker; a silent sentinel is the failure being avoided."""
    with pytest.raises(RuntimeError):
        find_repo_root(start=tmp_path)
    with pytest.raises(RuntimeError):
        find_server_root(start=tmp_path)
