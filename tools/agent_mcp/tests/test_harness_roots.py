"""The harness's own derivation of the two roots -- the one that collapsed (bug tj-fts1lo).

harness.py deliberately does not import common/tests/roots.py; its comment gives the two reasons,
both about the seam tools/ sits on. The cost of that is ANOTHER spelling of the same definition --
one of FOUR in this repository, not two: common/tests/roots.py (the canonical one), this harness,
tests/fakes/record_alpaca.py and server/data/store/run_migrations.sh. Every one of them is forced
by where it sits rather than chosen, so the control is conformance and not de-duplication:
common/tests/test_roots.py::test_every_derivation_of_the_two_roots_agrees enumerates all four and
holds each to the canonical one. This file is what holds THIS spelling's discriminating behaviour,
which the inventory cannot see: the inventory only asks whether today's checkout agrees.

WHY THIS SEARCH AND NOT THE OTHER ONE. roots.py walks UPWARD from server/common/tests, so it meets
server/ before the repository root and never examines what the root holds. This one steps DOWNWARD
and tries THE REPOSITORY ROOT FIRST, so anything at the root answering the marker beats the real
server/ beneath it. A checkout that predates tj-iontkq.4 keeps common/, routers/ and schemas/ at
the root holding only untracked __pycache__ -- `git mv` moves tracked files and nothing else. Those
shells answered the old `is_dir()` marker, _server_root returned the repository root, and the two
roots collapsed back into one. CI builds fresh and never reproduces it; a developer's checkout
reproduces it every time, which is the direction that hides a defect rather than the one that
surfaces it.

tools/agent_mcp/tests/test_seed_dump.py::test_the_refused_exit_status_is_the_producers was the only
loud symptom, and only because it names a file that genuinely moved. Every other caller of
SERVER_ROOT went on passing against the pre-move value.
"""

from pathlib import Path

import pytest

from tools.agent_mcp.tests.harness import (
    PACKAGE_MARKER,
    REPO_ROOT,
    SERVER_MARKERS,
    SERVER_ROOT,
    _repo_root,
    _server_root,
)


pytestmark = pytest.mark.build_infra

SERVER_DIR_NAME = 'server'


def _shell(parent: Path, name: str) -> None:
    """A stale leftover: the right directory name holding only untracked bytecode."""
    cache = parent / name / '__pycache__'
    cache.mkdir(parents=True)
    (cache / f'{name}.cpython-312.pyc').write_bytes(b'\x00')


def _package(parent: Path, name: str) -> None:
    """A real tree: the right directory name, carrying __init__.py."""
    (parent / name).mkdir(parents=True)
    (parent / name / PACKAGE_MARKER).write_text('', encoding='utf-8')


# --- the live checkout -------------------------------------------------------------------------


def test_the_two_roots_are_distinct_in_this_checkout():
    """THE MISSING PIN, from this side. Had it existed, tj-fts1lo would have been one red test."""
    assert SERVER_ROOT != REPO_ROOT


def test_the_server_root_is_the_server_directory_under_the_repository_root():
    assert SERVER_ROOT == REPO_ROOT / SERVER_DIR_NAME


def test_the_live_server_root_really_holds_the_three_packages():
    """Not merely a directory named server/: the thing the marker is supposed to mean."""
    assert all((SERVER_ROOT / name / PACKAGE_MARKER).is_file() for name in SERVER_MARKERS)


def test_the_repository_root_is_found_by_its_own_marker():
    assert _repo_root() == REPO_ROOT and (REPO_ROOT / 'pytest.ini').is_file()


# --- the marker discriminates --------------------------------------------------------------------


def test_stale_shells_at_the_repository_root_do_not_win_over_the_real_server_tree(tmp_path: Path):
    """THE REGRESSION, reproduced exactly as reported.

    Untracked leftovers at the old tree paths, the genuine packages under server/. Under the old
    `is_dir()` marker this returned tmp_path -- the collapse. It must return tmp_path/server.
    """
    for name in SERVER_MARKERS:
        _shell(tmp_path, name)
        _package(tmp_path / SERVER_DIR_NAME, name)
    assert _server_root(tmp_path) == tmp_path / SERVER_DIR_NAME


def test_a_repository_root_that_holds_the_packages_itself_is_still_the_server_root(tmp_path: Path):
    """The pre-move layout, and the branch the fix must not break: the root itself can qualify."""
    for name in SERVER_MARKERS:
        _package(tmp_path, name)
    assert _server_root(tmp_path) == tmp_path


def test_a_child_holding_only_some_of_the_packages_is_not_the_server_root(tmp_path: Path):
    """All three, so a half-finished move does not quietly nominate a root."""
    for name in SERVER_MARKERS[:-1]:
        _package(tmp_path / SERVER_DIR_NAME, name)
    _shell(tmp_path / SERVER_DIR_NAME, SERVER_MARKERS[-1])
    with pytest.raises(RuntimeError, match='packages'):
        _server_root(tmp_path)


def test_the_search_refuses_rather_than_falling_back(tmp_path: Path):
    """A sentinel that quietly returned the wrong directory is the vacuous green being avoided."""
    with pytest.raises(RuntimeError, match='packages'):
        _server_root(tmp_path)
