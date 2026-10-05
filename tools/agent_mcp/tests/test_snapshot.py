"""The snapshot: an MCP-owned copy of a worktree's sources, the only tree the daemon ever reads.

ADR tj-4rr0la addendum 5 ruling 1, addenda 6 (D8, D11, L1, L2) and 8. tj-c4mosr.5 items S3, S4, G2,
G5, T1, T2 and the 02:14 extras (a socket, a dangling link, an empty-directory flood, a path over
PATH_MAX). The runner-level halves (S5, S6, G3, G4) are in test_runner.py.

Every refusal is asserted with the live snapshot byte-identical and no source.new left behind: a
refused copy must change nothing the daemon could later read.
"""

import os
import shutil
import socket
import stat
import threading
from pathlib import Path

import pytest

from tools.agent_mcp import stack
from tools.agent_mcp.tests.harness import WORKTREE_FILES, build_worktree, tree_digest


pytestmark = pytest.mark.build_infra


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path]:
    """(worktree, stack_dir): a complete worktree, and a stack directory holding one good snapshot."""
    root = Path(os.path.realpath(tmp_path))
    worktree = build_worktree(root / 'worktree')
    stack_dir = root / 'stack'
    stack_dir.mkdir(mode=0o700)
    stack.refresh_snapshot(stack_dir, worktree)
    return worktree, stack_dir


def _expected_files(worktree: Path) -> set[str]:
    return {
        relative
        for relative in WORKTREE_FILES
        if any(relative == source or relative.startswith(source + '/') for source in stack.SNAPSHOT_SOURCES)
    }


def test_the_snapshot_holds_exactly_the_allow_listed_sources(tree: tuple[Path, Path]):
    worktree, stack_dir = tree
    snapshot = stack_dir / 'source'
    files = {str(path.relative_to(snapshot)) for path in snapshot.rglob('*') if path.is_file()}
    assert files == _expected_files(worktree)
    for relative in files:
        assert (snapshot / relative).read_bytes() == (worktree / relative).read_bytes()
    assert not (snapshot / 'Dockerfile').exists() and not (snapshot / 'README.md').exists()


def test_live_env_files_and_pycache_are_skipped_but_the_template_is_copied(tree: tuple[Path, Path]):
    """S3: a sentinel .env under common/ never reaches the snapshot; .env.default does."""
    worktree, stack_dir = tree
    for name in ('.env', '.env.local', '.env.production'):
        (worktree / 'server' / 'common' / name).write_text('SECRET=sentinel\n')
    (worktree / 'server' / 'common' / '__pycache__').mkdir()
    (worktree / 'server' / 'common' / '__pycache__' / 'x.pyc').write_bytes(b'\0')
    stack.refresh_snapshot(stack_dir, worktree)
    common = stack_dir / 'source' / 'server' / 'common'
    assert sorted(path.name for path in common.iterdir()) == ['.env.default', '__init__.py', 'sub']


def test_uncommitted_and_untracked_work_is_carried_over(tree: tuple[Path, Path]):
    """S4: the snapshot is of the WORKING TREE, so builders test before committing."""
    worktree, stack_dir = tree
    (worktree / 'server' / 'common' / 'sub' / 'module.py').write_text('VALUE = 2  # an uncommitted edit\n')
    (worktree / 'tests' / 'system' / 'test_new.py').write_text('def test_new():\n    pass\n')
    stack.refresh_snapshot(stack_dir, worktree)
    snapshot = stack_dir / 'source'
    assert (snapshot / 'server' / 'common' / 'sub' / 'module.py').read_text() == 'VALUE = 2  # an uncommitted edit\n'
    assert (snapshot / 'tests' / 'system' / 'test_new.py').exists()


def _refused_changes_nothing(
    stack_dir: Path, worktree: Path, match: str, cancel: threading.Event | None = None
) -> None:
    before = tree_digest(stack_dir / 'source')
    with pytest.raises(stack.Refused, match=match):
        stack.refresh_snapshot(stack_dir, worktree, cancel)
    assert tree_digest(stack_dir / 'source') == before, 'a refused copy changed the live snapshot'
    assert not os.path.lexists(stack_dir / 'source.new'), 'a refused copy left source.new behind (L1)'
    assert not os.path.lexists(stack_dir / 'source.old')


def _outside(worktree: Path) -> Path:
    outside = worktree.parent / 'outside'
    outside.mkdir(exist_ok=True)
    (outside / 'secret.txt').write_text('host secret\n')
    return outside


def test_a_symlink_at_the_top_of_an_entry_is_refused(tree: tuple[Path, Path]):
    worktree, stack_dir = tree
    target = _outside(worktree)
    (worktree / 'server' / 'routers').rename(worktree / 'server' / 'routers_real')
    (worktree / 'server' / 'routers').symlink_to(target)
    _refused_changes_nothing(stack_dir, worktree, 'server/routers is a symlink')


def test_a_symlink_to_a_file_deep_inside_an_entry_is_refused(tree: tuple[Path, Path]):
    worktree, stack_dir = tree
    (worktree / 'server' / 'common' / 'sub' / 'link.py').symlink_to(_outside(worktree) / 'secret.txt')
    _refused_changes_nothing(stack_dir, worktree, 'server/common/sub/link.py is a symlink')


def test_a_symlink_to_a_directory_deep_inside_an_entry_is_refused(tree: tuple[Path, Path]):
    """G2 / D11: os.fwalk(follow_symlinks=False) SKIPS a directory link silently; the lstat of dirnames refuses it."""
    worktree, stack_dir = tree
    (worktree / 'server' / 'common' / 'sub' / 'dirlink').symlink_to(_outside(worktree), target_is_directory=True)
    _refused_changes_nothing(stack_dir, worktree, 'server/common/sub/dirlink is a symlink')


def test_a_dangling_symlink_is_refused(tree: tuple[Path, Path]):
    worktree, stack_dir = tree
    (worktree / 'server' / 'schemas' / 'gone.py').symlink_to(worktree / 'nowhere.py')
    _refused_changes_nothing(stack_dir, worktree, 'server/schemas/gone.py is a symlink')


def test_a_symlinked_parent_of_a_nested_entry_is_refused(tree: tuple[Path, Path]):
    """data/store/app is an entry; a link at data/store must not be walked through either."""
    worktree, stack_dir = tree
    target = _outside(worktree)
    (worktree / 'server' / 'data' / 'store').rename(target / 'store')
    (worktree / 'server' / 'data' / 'store').symlink_to(target / 'store')
    _refused_changes_nothing(
        stack_dir, worktree, 'server/data/store/app: store is missing, a symlink or not a directory'
    )


def test_a_fifo_is_refused_without_blocking(tree: tuple[Path, Path]):
    worktree, stack_dir = tree
    os.mkfifo(worktree / 'tests' / 'system' / 'pipe')
    _refused_changes_nothing(stack_dir, worktree, 'tests/system/pipe is not a regular file')


def test_a_socket_is_refused(tree: tuple[Path, Path]):
    worktree, stack_dir = tree
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(worktree / 'server' / 'common' / 'sock'))
        _refused_changes_nothing(stack_dir, worktree, 'server/common/sock is not a regular file')
    finally:
        server.close()


def test_a_missing_entry_is_refused(tree: tuple[Path, Path]):
    worktree, stack_dir = tree
    (worktree / 'pytest.ini').unlink()
    _refused_changes_nothing(stack_dir, worktree, 'pytest.ini is missing from the worktree')


def test_the_byte_cap_is_enforced(tree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch):
    worktree, stack_dir = tree
    (worktree / 'server' / 'common' / 'big.bin').write_bytes(b'\0' * 4096)
    monkeypatch.setattr(stack, 'SNAPSHOT_MAX_BYTES', 2048)
    _refused_changes_nothing(stack_dir, worktree, 'cap of 2048 bytes')


def test_the_file_count_cap_is_enforced(tree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch):
    worktree, stack_dir = tree
    for index in range(30):
        (worktree / 'server' / 'routers' / f'm{index}.py').write_text('')
    monkeypatch.setattr(stack, 'SNAPSHOT_MAX_FILES', 25)
    _refused_changes_nothing(stack_dir, worktree, 'cap of 25 files')


def test_an_empty_directory_flood_hits_the_file_count_cap(tree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch):
    """02:14 (2): directories tick too, so a flood of empty ones cannot slip under the cap."""
    worktree, stack_dir = tree
    for index in range(40):
        (worktree / 'server' / 'schemas' / f'd{index}').mkdir()
    monkeypatch.setattr(stack, 'SNAPSHOT_MAX_FILES', 30)
    _refused_changes_nothing(stack_dir, worktree, 'cap of 30 files')


def test_a_path_over_path_max_is_refused_and_the_next_refresh_recovers(tree: tuple[Path, Path]):
    """02:14 (2): Refused, not an uncaught OSError; the next refresh, with the tree fixed, succeeds."""
    worktree, stack_dir = tree
    deep = worktree / 'server' / 'common'
    fd = os.open(deep, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for _ in range(24):
            name = 'd' * 200
            os.mkdir(name, dir_fd=fd)
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        with open(os.open('leaf.py', os.O_WRONLY | os.O_CREAT, 0o644, dir_fd=fd), 'w') as handle:
            handle.write('')
    finally:
        os.close(fd)
    _refused_changes_nothing(stack_dir, worktree, 'common')

    shutil.rmtree(worktree / 'server' / 'common' / ('d' * 200))
    stack.refresh_snapshot(stack_dir, worktree)
    assert (stack_dir / 'source' / 'server' / 'common' / '__init__.py').exists()


# --- T1 / L1: a failed copy leaves no source.new and the live snapshot as it was -------------------


def test_a_refused_copy_under_tests_system_leaves_nothing(tree: tuple[Path, Path]):
    worktree, stack_dir = tree
    os.mkfifo(worktree / 'tests' / 'system' / 'fifo')
    _refused_changes_nothing(stack_dir, worktree, 'not a regular file')


def test_a_preset_cancel_leaves_nothing(tree: tuple[Path, Path]):
    worktree, stack_dir = tree
    cancel = threading.Event()
    cancel.set()
    _refused_changes_nothing(stack_dir, worktree, 'cancelled', cancel)


@pytest.mark.parametrize('error', [OSError('disk'), KeyboardInterrupt()], ids=['OSError', 'KeyboardInterrupt'])
def test_a_verify_failure_of_any_kind_leaves_nothing(
    tree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, error: BaseException
):
    worktree, stack_dir = tree
    before = tree_digest(stack_dir / 'source')

    def failing_verify(snapshot: Path) -> None:
        raise error

    monkeypatch.setattr(stack, 'verify_snapshot', failing_verify)
    with pytest.raises(type(error)):
        stack.refresh_snapshot(stack_dir, worktree)
    assert tree_digest(stack_dir / 'source') == before
    assert not os.path.lexists(stack_dir / 'source.new')


def test_a_cleanup_failure_still_surfaces_the_original_refusal(
    tree: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
):
    worktree, stack_dir = tree
    os.mkfifo(worktree / 'tests' / 'system' / 'fifo')
    real = stack._remove_generation
    calls = []

    def remove(path: Path) -> None:
        calls.append(path)
        if len(calls) > 2:  # the two leftovers cleared first succeed; the cleanup after the refusal fails
            raise OSError('cleanup failed')
        real(path)

    monkeypatch.setattr(stack, '_remove_generation', remove)
    with pytest.raises(stack.Refused, match='tests/system/fifo is not a regular file'):
        stack.refresh_snapshot(stack_dir, worktree)
    assert calls[-1] == stack_dir / 'source.new'


# --- T2 / L2 / G5: directory modes, and no link followed at the path itself ------------------------


@pytest.fixture
def umask_077():
    previous = os.umask(0o077)
    yield
    os.umask(previous)


def test_under_umask_077_directories_are_0755_and_files_keep_their_exec_bit(tmp_path: Path, umask_077: None):
    """G5: the stack's containers enter the snapshot as their own uids, so directories are 0755."""
    root = Path(os.path.realpath(tmp_path))
    worktree = build_worktree(root / 'worktree')
    stack_dir = root / 'stack'
    stack_dir.mkdir()
    snapshot = stack.refresh_snapshot(stack_dir, worktree)
    modes = {str(path.relative_to(snapshot)): stat.S_IMODE(path.lstat().st_mode) for path in snapshot.rglob('*')}
    modes['.'] = stat.S_IMODE(snapshot.lstat().st_mode)
    wrong = {
        relative: oct(mode)
        for relative, mode in modes.items()
        if mode
        != (0o755 if relative == '.' or (snapshot / relative).is_dir() or relative == 'entrypoint.sh' else 0o644)
    }
    assert not wrong, f'snapshot modes under umask 077: {wrong}'


def test_make_snapshot_dir_creates_0755_at_every_level(tmp_path: Path, umask_077: None):
    """T2 / J1: parents created with parents=True are 0755 too, not the umask's 0700."""
    base = Path(os.path.realpath(tmp_path))
    stack._make_snapshot_dir(base / 'a' / 'b' / 'c', parents=True)
    for path in (base / 'a', base / 'a' / 'b', base / 'a' / 'b' / 'c'):
        assert stat.S_IMODE(path.lstat().st_mode) == 0o755, path


def test_make_snapshot_dir_brings_an_existing_0700_directory_to_0755(tmp_path: Path):
    path = tmp_path / 'existing'
    path.mkdir(mode=0o700)
    stack._make_snapshot_dir(path)
    assert stat.S_IMODE(path.lstat().st_mode) == 0o755


def test_make_snapshot_dir_refuses_a_symlink_to_a_directory_and_leaves_its_target_alone(tmp_path: Path):
    """T2 / L2: the mode is set on an O_NOFOLLOW descriptor, so a planted link's target is never chmod'd."""
    target = tmp_path / 'private'
    target.mkdir(mode=0o700)
    link = tmp_path / 'link'
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(stack.Refused, match='symlink or not a directory'):
        stack._make_snapshot_dir(link)
    assert stat.S_IMODE(target.lstat().st_mode) == 0o700, 'a link target was chmod-ed through the link'


def test_make_snapshot_dir_refuses_a_dangling_link_and_creates_nothing(tmp_path: Path):
    target = tmp_path / 'would_be_created'
    link = tmp_path / 'link'
    link.symlink_to(target)
    with pytest.raises(stack.Refused):
        stack._make_snapshot_dir(link)
    assert not target.exists()


def test_make_snapshot_dir_refuses_a_regular_file(tmp_path: Path):
    path = tmp_path / 'file'
    path.write_text('x')
    path.chmod(0o600)
    with pytest.raises(stack.Refused):
        stack._make_snapshot_dir(path)
    assert stat.S_IMODE(path.lstat().st_mode) == 0o600


# --- verify_snapshot and the empty snapshot ---------------------------------------------------------


def test_verify_refuses_a_link_or_special_file_anywhere_and_a_missing_entry(tree: tuple[Path, Path]):
    _, stack_dir = tree
    snapshot = stack_dir / 'source'
    stack.verify_snapshot(snapshot)
    (snapshot / 'server' / 'common' / 'link').symlink_to('/etc')
    with pytest.raises(stack.Refused, match='server/common/link'):
        stack.verify_snapshot(snapshot)
    (snapshot / 'server' / 'common' / 'link').unlink()
    (snapshot / 'uv.lock').unlink()
    with pytest.raises(stack.Refused, match=r'uv\.lock is missing'):
        stack.verify_snapshot(snapshot)


def test_ensure_snapshot_reads_no_worktree_and_replaces_a_planted_link(tmp_path: Path):
    stack_dir = Path(os.path.realpath(tmp_path)) / 'stack'
    stack_dir.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (stack_dir / 'source').symlink_to(outside, target_is_directory=True)
    live = stack.ensure_snapshot(stack_dir)
    assert live == stack_dir / 'source' and live.is_dir() and not live.is_symlink()
    assert list(live.iterdir()) == [] and outside.exists()
