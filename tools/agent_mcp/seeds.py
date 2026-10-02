"""seed_dump's own half of the seed bundle: the MCP's reader, and its no-follow writer into the share directory.

THE READER. The producer (data.store.seeds, run in test_client) prints ONE bundle line on stdout;
the contract is written in data/store/seeds/bundle.py's docstring (decision tj-vhboky.55 S9 (3),
S10.3 N1). The MCP cannot import data.store -- its image carries tools/agent_mcp and nothing else
(ADR tj-4rr0la addendum 10 (1)) -- so parse_bundle implements the same contract a second time, and
a cross-contract test (tj-irhy0a.22 V2) pins the two readers to one table of cases. Change both as
one. Every refusal message names the rule broken and carries no content of the bundle.

THE WRITER. write_seed puts <revision>.sql and <revision>.json under
<share>/SEEDS_DIR_NAME/<worktree name>/, where <share> is the server's AGENT_HOME_PATH setting --
/agent_mcp_share in agent_mcp, bound at the same path in the devcontainer (ADR tj-4rr0la addendum 10
(3), relocated by addendum 11 R2), so a path the response names opens as-is there. The devcontainer
can write that directory, so the algorithm never follows a link an agent planted and never writes
through an existing inode: an O_DIRECTORY|O_NOFOLLOW fd of the share root, then each component
opened the same way relative to the last (a missing one made with mkdirat; a symlink, a file or
anything but a plain directory refused); each file written to a fresh O_CREAT|O_EXCL|O_NOFOLLOW name
and fsync'd; and only when BOTH are written, both renamed over their targets by directory fd. A
symlink or hard link planted at a target is replaced by the rename, its own target untouched. A
directory planted at a target is refused before either rename; one planted concurrently between
that check and the second rename leaves half a pair and raises (the residual decision tj-vhboky.55
S11.1 accepts for the host writer, which uses the same algorithm).

Standard library only.
"""

import contextlib
import errno
import json
import os
import re
import secrets
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


# The contract's constants, as data/store/seeds/bundle.py spells them.
BUNDLE_TAG = 'trader_joe-seed/1'
BUNDLE_KEYS = frozenset({'bundle', 'revision', 'sql', 'manifest'})
REVISION_PATTERN = re.compile(r'^[0-9a-f]{12}$')
# Counted in bytes over the whole of stdout, newline included. Also the runner's read limit for the
# producer's stdout (stack.seed_dump_steps): it keeps one byte more, so an over-cap stdout is seen as
# over the cap without holding all of it.
MAX_BUNDLE_BYTES = 8 * 1024 * 1024

# Under the share root: <share>/agent_mcp_seeds/<worktree name>/<revision>.{sql,json}.
SEEDS_DIR_NAME = 'agent_mcp_seeds'

_ROOT_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_NEW_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
# Owned by the one UID the host, the devcontainer user and agent_mcp all share (make agent-mcp-up runs
# the server as id -u/-g; the 0600 token file depends on the same). The modes are for that owner, and
# the share root's 0700 keeps everyone else out, so no fchmod is needed whatever the server's umask.
_FILE_MODE = 0o644
_DIR_MODE = 0o755
_TABLE_NAME = re.compile(r'^[a-z_][a-z0-9_]{0,62}$')


class BundleRefused(Exception):
    """A bundle that breaks the contract, or an output path that is refused. The message carries no bundle content."""


@dataclass(frozen=True)
class SeedBundle:
    """One seed, as the contract accepted it.

    Args:
        revision (str): The schema revision, 12 lower-case hex characters.
        sql (str): The <revision>.sql text, ending in one newline.
        manifest (str): The <revision>.json text, ending in one newline.
    """

    revision: str
    sql: str
    manifest: str


@dataclass(frozen=True)
class SeedFiles:
    """What write_seed wrote: paths RELATIVE to the share root, byte sizes, and the manifest's row counts.

    Never the content. row_counts is None when the manifest's row_counts is not a mapping of table
    names to non-negative integers.
    """

    sql: str
    manifest: str
    sql_bytes: int
    manifest_bytes: int
    row_counts: dict[str, int] | None


def _refuse_repeated_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """json.loads object_pairs_hook: an object naming a key twice is refused; the message carries no key or value."""
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise BundleRefused('a JSON object names a key more than once')
    return dict(pairs)


def _ends_in_one_newline(text: object) -> bool:
    return isinstance(text, str) and bool(text.strip()) and text.endswith('\n') and not text.endswith('\n\n')


def parse_bundle(stdout: bytes) -> SeedBundle:
    """Read the producer's whole stdout under the bundle contract (data/store/seeds/bundle.py's docstring).

    Args:
        stdout (bytes): Everything the producer printed on stdout.

    Returns:
        SeedBundle: The seed.

    Raises:
        BundleRefused: On anything the contract refuses: over MAX_BUNDLE_BYTES (checked before any
            parsing), not UTF-8, no non-empty line, a last line that is not JSON, a repeated key at
            either level, keys other than exactly bundle/revision/sql/manifest, a wrong tag, a
            revision that is not 12 lower-case hex or differs from the manifest's, sql or manifest
            not a non-empty string ending in exactly one newline, a manifest that is not a JSON
            object. The message carries no content.
    """
    if len(stdout) > MAX_BUNDLE_BYTES:
        raise BundleRefused(f'the output is over the {MAX_BUNDLE_BYTES}-byte cap')
    try:
        text = stdout.decode('utf-8')
    except UnicodeDecodeError:
        raise BundleRefused('the output is not UTF-8') from None
    lines = [line for line in text.split('\n') if line.strip()]
    if not lines:
        raise BundleRefused('the output holds no bundle line')
    try:
        parsed = json.loads(lines[-1], object_pairs_hook=_refuse_repeated_keys)
    except ValueError:
        raise BundleRefused('the last output line is not JSON') from None
    if not isinstance(parsed, dict) or set(parsed) != BUNDLE_KEYS:
        raise BundleRefused('the bundle is not an object with exactly the keys bundle, revision, sql, manifest')
    if parsed['bundle'] != BUNDLE_TAG:
        raise BundleRefused('the bundle tag is not the expected literal')
    revision, sql, manifest = parsed['revision'], parsed['sql'], parsed['manifest']
    if not isinstance(revision, str) or not REVISION_PATTERN.fullmatch(revision):
        raise BundleRefused('the revision is not 12 lower-case hex characters')
    if not _ends_in_one_newline(sql) or not _ends_in_one_newline(manifest):
        raise BundleRefused('sql and manifest must be non-empty strings each ending in exactly one newline')
    try:
        manifest_revision = json.loads(manifest, object_pairs_hook=_refuse_repeated_keys).get('revision')
    except (ValueError, AttributeError):
        raise BundleRefused('the manifest is not a JSON object') from None
    if manifest_revision != revision:
        raise BundleRefused('the revision differs from the manifest revision')
    return SeedBundle(revision, sql, manifest)


def manifest_row_counts(manifest: str) -> dict[str, int] | None:
    """The manifest's row_counts when it is a mapping of plain table names to non-negative ints, else None.

    Only names that look like a table name and plain integers pass, so nothing the producer chose to
    put there beyond a count reaches the response.
    """
    try:
        counts = json.loads(manifest).get('row_counts')
    except (ValueError, AttributeError):
        return None
    if not isinstance(counts, dict):
        return None
    for name, count in counts.items():
        if not _TABLE_NAME.fullmatch(name) or isinstance(count, bool) or not isinstance(count, int) or count < 0:
            return None
    return dict(counts)


def _open_component(parent_fd: int, name: str) -> int:
    """Open one directory beneath parent_fd without following a link, making it (mkdirat) when absent.

    Raises:
        BundleRefused: If the component exists as a symlink or anything but a directory.
    """
    for _ in range(2):
        try:
            return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError:
            # A concurrent maker may win the gap; the next pass opens what it made, still no-follow.
            with contextlib.suppress(FileExistsError):
                os.mkdir(name, _DIR_MODE, dir_fd=parent_fd)
        except OSError as failure:
            if failure.errno in (errno.ELOOP, errno.ENOTDIR):
                raise BundleRefused('a seed directory component is a symlink or not a directory') from None
            raise
    raise BundleRefused('a seed directory component could not be opened as a plain directory')


def _walk(share_root: Path, parts: Sequence[str]) -> int:
    """An O_DIRECTORY|O_NOFOLLOW fd of the share root, then each part the same way. Returns the last fd."""
    try:
        fd = os.open(share_root, _ROOT_FLAGS)
    except OSError as failure:
        if failure.errno in (errno.ELOOP, errno.ENOTDIR):
            raise BundleRefused('the share directory is a symlink or not a directory') from None
        raise
    try:
        for part in parts:
            child = _open_component(fd, part)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _write_new(dir_fd: int, final: str, data: bytes) -> str:
    """Write data to a fresh, exclusively created name in the directory, fsync'd; return that name."""
    name = f'.{final}.{secrets.token_hex(8)}.tmp'
    fd = os.open(name, _NEW_FILE_FLAGS, _FILE_MODE, dir_fd=dir_fd)
    try:
        view = memoryview(data)
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
    """Before any rename, refuse a target that exists and is neither a regular file nor a symlink (lstat).

    A rename over a directory fails, and it could fail after the first target was replaced; looking
    first keeps the pair whole.
    """
    for name in names:
        try:
            mode = os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode
        except FileNotFoundError:
            continue
        if not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
            raise BundleRefused('a seed target exists and is neither a file nor a symlink')


def _check_worktree_dir_name(name: str) -> None:
    """The worktree name is one plain path component (the runner has already checked its shape)."""
    if not name or '/' in name or '\0' in name or name in ('.', '..'):
        raise BundleRefused('the worktree name is not a single path component')


def write_seed(share_root: Path, worktree_name: str, bundle: SeedBundle) -> SeedFiles:
    """Write <revision>.sql and <revision>.json under <share_root>/SEEDS_DIR_NAME/<worktree_name>/, no-follow.

    The algorithm is the module docstring's. A failure before the renames leaves neither target
    and no temporary.

    Args:
        share_root (Path): The server's AGENT_HOME_PATH setting (the share directory).
        worktree_name (str): The worktree the seed was produced from, already validated.
        bundle (SeedBundle): A bundle parse_bundle accepted.

    Returns:
        SeedFiles: The two paths relative to share_root, their sizes and the manifest's row counts.

    Raises:
        BundleRefused: If the share root or a component below it is a symlink or not a directory, or a
            target is a directory or special file; nothing is written.
        OSError: Any other OS failure (permissions, a full disk); temporaries are removed.
    """
    _check_worktree_dir_name(worktree_name)
    sql_name, manifest_name = f'{bundle.revision}.sql', f'{bundle.revision}.json'
    sql_data, manifest_data = bundle.sql.encode('utf-8'), bundle.manifest.encode('utf-8')
    dir_fd = _walk(share_root, (SEEDS_DIR_NAME, worktree_name))
    temporaries: dict[str, str] = {}
    try:
        # Both temporaries exist before either rename, so a failure writing the second leaves no target.
        temporaries[sql_name] = _write_new(dir_fd, sql_name, sql_data)
        temporaries[manifest_name] = _write_new(dir_fd, manifest_name, manifest_data)
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
    relative = f'{SEEDS_DIR_NAME}/{worktree_name}'
    return SeedFiles(
        sql=f'{relative}/{sql_name}',
        manifest=f'{relative}/{manifest_name}',
        sql_bytes=len(sql_data),
        manifest_bytes=len(manifest_data),
        row_counts=manifest_row_counts(bundle.manifest),
    )
