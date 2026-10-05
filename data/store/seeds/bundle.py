r"""The seed bundle: the ONE line the producer prints on stdout, and its two halves.

The producer writes nothing to disk. On success its stdout holds exactly one line, a JSON object,
and everything else it says goes to stderr. Decision tj-vhboky.55 addendum S9 (3); ADR tj-4rr0la
addendum 10.

THE CONTRACT. It is implemented TWICE: here (host side, `make seed-dump`, via parse_bundle and
write_bundle) and in tools/agent_mcp, which cannot import this package and has its own reader. A
cross-contract test (tj-irhy0a.22 V2) pins the two together, so change this and that reader as one.
A consumer must:

  * refuse stdout longer than MAX_BUNDLE_BYTES (8 MiB, counted in UTF-8 bytes over the whole of
    stdout, newline included) without parsing it;
  * take the LAST non-empty stdout line as the bundle and parse it as JSON;
  * refuse an object that names any key more than once, at the top level or inside the manifest
    (a repeated key is refused, never last-wins);
  * require an object with EXACTLY the four keys "bundle", "revision", "sql" and "manifest";
  * require "bundle" to equal the literal BUNDLE_TAG, 'trader_joe-seed/1';
  * require "revision" to be a string matching ^[0-9a-f]{12}$ (REVISION_PATTERN) AND to equal the
    "revision" field of the manifest, which is JSON text;
  * require "sql" and "manifest" to be non-empty strings, each ending in exactly one newline;
  * refuse anything else, with a message that carries no content of the bundle;
  * write the two files as <revision>.sql and <revision>.json, holding "sql" and "manifest"
    verbatim (UTF-8, newlines unchanged), and write nothing at all on any refusal.

THE HOST WRITER (write_bundle) follows ADR tj-4rr0la addendum 10 (3), because it runs as the user in
a directory an agent can modify. It never follows a symlink and never opens an existing target for
writing: it walks the output directory from a named root one component at a time with
O_DIRECTORY|O_NOFOLLOW (a missing component is made with mkdirat; a symlink or a file where a
directory belongs is refused), writes each file to a fresh O_CREAT|O_EXCL|O_NOFOLLOW name in that
directory, fsyncs it, and only when BOTH temporaries are written renames them over the targets by
directory fd, so a symlink or hard link planted at <revision>.sql or .json is replaced, its target
untouched. A failure before the renames leaves neither target and no temporary.

Host half: `python -m data.store.seeds.bundle --out DIR [--allow-tests-dir]` reads the producer's
stdout on stdin and writes the two files. A directory inside the repository's tests/ is refused
unless --allow-tests-dir is passed, because the make target and the MCP write into a git-ignored
directory and a person copies a reviewed seed into tests/system/seeds/ (tj-vhboky.56).

This module imports the standard library only.
"""

import argparse
import contextlib
import errno
import json
import os
import re
import secrets
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


# REPO_ROOT -- the TRUE repository root, found by searching upward for pytest.ini rather than by
# counting parent directories (tj-qanatv; epic tj-iontkq risk R-1, fixed for the test tree by
# tj-iontkq.2's common/tests/roots.py).
#
# check_out_dir's whole job is refusing an output directory under <repo>/tests, and its docstring
# says the check runs on the resolved path too, so the root it compares against must be the root
# that actually HOLDS tests/ in whatever layout is checked out -- not whichever directory happens
# to sit a fixed distance above this file. The old `Path(__file__).resolve().parents[3]` was that
# fixed distance: correct only because data/store/seeds sits three levels under the repository
# root today. Once the service trees move a level down (epic tj-iontkq), parents[3] resolves to
# the new server/ directory, server/tests/ does not exist, and the refusal stops firing with no
# error at all -- the one upward-counted root in this epic that fails OPEN instead of closed.
#
# NOT common/tests/roots.py's sentinel: this module imports the standard library only
# (see test_the_bundle_module_imports_the_standard_library_only) and production code must not
# import from a tests package, so the search is reproduced locally rather than shared. This is
# the only production site that needs a repository root.
_REPO_MARKER = 'pytest.ini'


def _find_repo_root(start: Path) -> Path:
    """The nearest ancestor of START holding pytest.ini -- the true repository root.

    Raising when nothing matches is the point: a sentinel that silently fell back to a default or
    to the filesystem root would be the same vacuous-pass failure in a new place, since every path
    built from it would exist nowhere.

    Raises:
        RuntimeError: If no ancestor of START, START included, carries pytest.ini.
    """
    for candidate in (start, *start.parents):
        if (candidate / _REPO_MARKER).is_file():
            return candidate
    raise RuntimeError(
        f'no ancestor of {start} carries {_REPO_MARKER}, so the repository root cannot be located. '
        f'Searched {start} and its {len(start.parents)} parents up to {start.anchor!r}.'
    )


REPO_ROOT = _find_repo_root(Path(__file__).resolve().parent)

BUNDLE_TAG = 'trader_joe-seed/1'
BUNDLE_KEYS = frozenset({'bundle', 'revision', 'sql', 'manifest'})
REVISION_PATTERN = re.compile(r'^[0-9a-f]{12}$')
MAX_BUNDLE_BYTES = 8 * 1024 * 1024

EXIT_REFUSED = 3
EXIT_FAILED = 1


class BundleRefused(Exception):
    """A bundle that breaks the contract, or an output directory that is refused. The message carries no bundle content."""


def _refuse_repeated_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """json.loads object_pairs_hook: an object naming a key twice is refused; the message carries no key or value."""
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise BundleRefused('a JSON object names a key more than once')
    return dict(pairs)


@dataclass(frozen=True)
class Bundle:
    """One seed.

    Args:
        revision (str): The schema revision, 12 lower-case hex characters.
        sql (str): The <revision>.sql text, ending in one newline.
        manifest (str): The <revision>.json text, ending in one newline.
    """

    revision: str
    sql: str
    manifest: str


def _ends_in_one_newline(text: object) -> bool:
    return isinstance(text, str) and bool(text.strip()) and text.endswith('\n') and not text.endswith('\n\n')


def _validate(bundle: object) -> Bundle:
    """The shared checks, for a parsed object or a Bundle about to be printed."""
    if isinstance(bundle, Bundle):
        revision, sql, manifest = bundle.revision, bundle.sql, bundle.manifest
    else:
        if not isinstance(bundle, dict) or set(bundle) != BUNDLE_KEYS:
            raise BundleRefused('the bundle is not an object with exactly the keys bundle, revision, sql, manifest')
        if bundle['bundle'] != BUNDLE_TAG:
            raise BundleRefused('the bundle tag is not the expected literal')
        revision, sql, manifest = bundle['revision'], bundle['sql'], bundle['manifest']
    if not isinstance(revision, str) or not REVISION_PATTERN.fullmatch(revision):
        raise BundleRefused('the revision is not 12 lower-case hex characters')
    if not _ends_in_one_newline(sql) or not _ends_in_one_newline(manifest):
        raise BundleRefused('sql and manifest must be non-empty strings each ending in exactly one newline')
    try:
        manifest_revision = json.loads(str(manifest), object_pairs_hook=_refuse_repeated_keys).get('revision')
    except (ValueError, AttributeError):
        raise BundleRefused('the manifest is not a JSON object') from None
    if manifest_revision != revision:
        raise BundleRefused('the revision differs from the manifest revision')
    return Bundle(revision, str(sql), str(manifest))


def render_bundle(bundle: Bundle) -> str:
    """The bundle as the one stdout line (without its newline).

    Args:
        bundle (Bundle): The seed.

    Returns:
        str: One line of JSON.

    Raises:
        BundleRefused: If the bundle breaks the contract or exceeds MAX_BUNDLE_BYTES.
    """
    _validate(bundle)
    line = json.dumps(
        {'bundle': BUNDLE_TAG, 'revision': bundle.revision, 'sql': bundle.sql, 'manifest': bundle.manifest}
    )
    if len(line.encode('utf-8')) + 1 > MAX_BUNDLE_BYTES:
        raise BundleRefused(f'the bundle is over the {MAX_BUNDLE_BYTES}-byte cap')
    return line


def parse_bundle(text: str) -> Bundle:
    """Parse the producer's stdout per the contract in the module docstring.

    Args:
        text (str): The whole of the producer's stdout.

    Returns:
        Bundle: The seed.

    Raises:
        BundleRefused: On anything the contract refuses, a repeated key included. The message carries no
            content of the text.
    """
    if len(text.encode('utf-8')) > MAX_BUNDLE_BYTES:
        raise BundleRefused(f'the output is over the {MAX_BUNDLE_BYTES}-byte cap')
    lines = [line for line in text.split('\n') if line.strip()]
    if not lines:
        raise BundleRefused('the output holds no bundle line')
    try:
        parsed = json.loads(lines[-1], object_pairs_hook=_refuse_repeated_keys)
    except ValueError:
        raise BundleRefused('the last output line is not JSON') from None
    return _validate(parsed)


def check_out_dir(out_dir: Path, allow_tests_dir: bool = False, repo_root: Path = REPO_ROOT) -> Path:
    """Resolve the output directory and refuse the repository's tests/ unless allowed.

    The check runs on the path as given (made absolute, not resolved) AND on the resolved path, so
    neither a spelling nor a symlink reaches tests/ unannounced.

    Args:
        out_dir (Path): The directory asked for.
        allow_tests_dir (bool): Permit a directory under <repo>/tests.
        repo_root (Path): The repository root.

    Returns:
        Path: The resolved directory.

    Raises:
        BundleRefused: If it lies under <repo>/tests and allow_tests_dir is False.
    """
    resolved = out_dir.resolve()
    if not allow_tests_dir:
        tests_dir = repo_root / 'tests'
        given = Path(os.path.abspath(out_dir))
        if given.is_relative_to(tests_dir) or resolved.is_relative_to(tests_dir.resolve()):
            raise BundleRefused(
                'the output directory is under tests/; pass --allow-tests-dir to write there on purpose'
            )
    return resolved


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_NEW_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_MODE = 0o644
_DIR_MODE = 0o755


def _open_component(parent_fd: int, name: str) -> int:
    """Open one directory component beneath parent_fd without following a symlink, making it when absent.

    Raises:
        BundleRefused: If the component exists and is a symlink or anything but a directory.
    """
    for _ in range(2):
        try:
            return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            # A concurrent maker may win the gap; the next pass opens it, still no-follow.
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, _DIR_MODE, dir_fd=parent_fd)
        except OSError as failure:
            if failure.errno in (errno.ELOOP, errno.ENOTDIR):
                raise BundleRefused('an output directory component is a symlink or not a directory') from None
            raise
    raise BundleRefused('an output directory component could not be opened as a plain directory')


def _walk_to(root: Path, parts: Sequence[str]) -> int:
    """Open root (following it, once: it is the trusted start), then each part no-follow. Returns the last fd."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in parts:
            child = _open_component(fd, part)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _write_new(dir_fd: int, prefix: str, text: str) -> str:
    """Write text to a fresh, exclusively created name in the directory; return that name."""
    name = f'.{prefix}.{secrets.token_hex(8)}.tmp'
    fd = os.open(name, _NEW_FILE_FLAGS, _FILE_MODE, dir_fd=dir_fd)
    try:
        view = memoryview(text.encode('utf-8'))
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        os.unlink(name, dir_fd=dir_fd)
        raise
    os.close(fd)
    return name


def _refuse_obstacles(dir_fd: int, names: Sequence[str]) -> None:
    """Refuse, before any rename, a target that is neither missing, a regular file nor a symlink.

    A rename over a directory fails, and it can fail AFTER the first target was replaced; looking
    first (lstat, so a symlink is judged as itself) keeps the pair whole.

    Raises:
        BundleRefused: If a target is a directory or any other special file. No path or content.
    """
    for name in names:
        try:
            mode = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
        except FileNotFoundError:
            continue
        if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
            raise BundleRefused('an output target exists and is neither a file nor a symlink')


def write_bundle(
    bundle: Bundle, out_dir: Path, allow_tests_dir: bool = False, repo_root: Path = REPO_ROOT, root: Path | None = None
) -> tuple[Path, Path]:
    """Write <revision>.sql and <revision>.json into a directory, after every check, never following a symlink.

    The algorithm is ADR tj-4rr0la addendum 10 (3); see the module docstring. THE WALK'S ROOT is the
    one directory trusted to be real: opened once, following links, because a person or a config
    chose it. Every component of out_dir BELOW it is opened with O_NOFOLLOW, so a symlink planted
    there is refused rather than followed. The default root is repo_root when out_dir lies under it
    (the git-ignored output/ case), otherwise the filesystem root. A symlink in a parent ABOVE the
    root, such as a symlinked checkout path, is the person's own and is followed. Pass root to
    narrow the trusted prefix. The output directory itself is a parameter; nothing here names a
    location.

    ALL-OR-NOTHING, AND ITS ONE RESIDUAL. Both temporaries are written, then both targets are
    lstat-ed and refused (BundleRefused, exit 3) unless missing, a regular file or a symlink, and
    only then are the two renames made; a failure before the first rename leaves nothing renamed.
    The check is not atomic with the renames: a directory planted at the second target AFTER the
    check and before its rename (by a concurrent writer of this directory) still lets the first
    rename land and the second fail, half a pair (OSError, exit 1; temporaries removed). The check
    narrows that window to those instants; it does not close it. It is not "impossible by
    construction"; the architect rules on whether it needs closing.

    Args:
        bundle (Bundle): The seed.
        out_dir (Path): Where to write.
        allow_tests_dir (bool): Permit a directory under <repo>/tests.
        repo_root (Path): The repository root.
        root (Path | None): The trusted start of the no-follow walk; out_dir must lie under it.

    Returns:
        tuple[Path, Path]: The .sql and the .json written, under out_dir made absolute.

    Raises:
        BundleRefused: If the bundle breaks the contract, the directory is refused, or a component of it
            is a symlink or not a directory; nothing is written. Any other OS failure (permissions, a
            full disk) raises OSError, which main maps to exit 1; temporaries are removed either way.
    """
    checked = _validate(bundle)
    check_out_dir(out_dir, allow_tests_dir, repo_root)
    absolute = Path(os.path.abspath(out_dir))
    if root is None:
        root = repo_root if absolute.is_relative_to(repo_root) else Path(absolute.anchor)
    if not absolute.is_relative_to(root):
        raise BundleRefused('the output directory is not under the walk root')
    dir_fd = _walk_to(root, absolute.relative_to(root).parts)
    temporaries: dict[str, str] = {}
    try:
        # Both temporaries exist before either rename, so a failure writing the second leaves no target.
        temporaries[f'{checked.revision}.sql'] = _write_new(dir_fd, f'{checked.revision}.sql', checked.sql)
        temporaries[f'{checked.revision}.json'] = _write_new(dir_fd, f'{checked.revision}.json', checked.manifest)
        _refuse_obstacles(dir_fd, list(temporaries))
        for final in list(temporaries):
            os.rename(temporaries[final], final, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            del temporaries[final]
        os.fsync(dir_fd)
    finally:
        for leftover in temporaries.values():
            # Already gone is fine; the original failure is the one to report.
            with contextlib.suppress(OSError):
                os.unlink(leftover, dir_fd=dir_fd)
        os.close(dir_fd)
    return absolute / f'{checked.revision}.sql', absolute / f'{checked.revision}.json'


def main(argv: Sequence[str] | None = None, stdin: str | None = None) -> int:
    """Read the producer's stdout on stdin and write the seed files.

    Args:
        argv (Sequence[str] | None): Arguments; sys.argv when None.
        stdin (str | None): The text to parse; read from sys.stdin when None.

    Returns:
        int: 0 the files were written; 3 the bundle or the directory was refused; 1 the write failed
            for any other OS reason (nothing renamed, temporaries removed).
    """
    parser = argparse.ArgumentParser(prog='python -m data.store.seeds.bundle', description='Write a seed bundle.')
    parser.add_argument('--out', required=True, type=Path, help='directory to write <revision>.sql and .json into')
    parser.add_argument('--allow-tests-dir', action='store_true', help='permit --out under the repository tests/')
    args = parser.parse_args(argv)
    text = sys.stdin.read() if stdin is None else stdin
    try:
        sql_path, manifest_path = write_bundle(parse_bundle(text), args.out, args.allow_tests_dir)
    except BundleRefused as refusal:
        print(f'seed bundle refused: {refusal}', file=sys.stderr)
        return EXIT_REFUSED
    except OSError as failure:
        code = errno.errorcode.get(failure.errno or 0, 'unknown')
        print(f'seed bundle write failed: {type(failure).__name__} ({code})', file=sys.stderr)
        return EXIT_FAILED
    print(f'wrote {sql_path} and {manifest_path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
