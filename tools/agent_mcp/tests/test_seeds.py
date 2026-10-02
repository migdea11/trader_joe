"""tools/agent_mcp/seeds.py: the MCP's own bundle reader and its no-follow writer into the share directory.

tj-irhy0a.22 V2 and V3. Design: ADR tj-4rr0la addendum 10 (3), relocated by addendum 11 R2;
decision tj-vhboky.55 S9 (3), S10.3 N1 and S11.

V2, THE CROSS-CONTRACT PIN. The MCP cannot import data.store, so the bundle contract is implemented
twice. ONE table -- data/store/tests/test_seed_bundle.py's CONTRACT_CASES, imported, never copied --
is run against BOTH readers here: each accepted case parses to exactly the listed files under both,
each refused case is refused by both, with a message that carries no bundle content. A drift in
either reader reds its half of the table.

V3, THE WRITER. Everything in a tmp_path tree: a share root, a victim directory and file outside it.
A symlink or a file planted at any component is refused; a symlink or hard link planted at a target
is replaced by the rename and its own target is byte-identical afterwards; nothing is written
outside <share>/agent_mcp_seeds.
"""

import ast
import errno
import json
import os
import stat
from pathlib import Path

import pytest

from data.store.seeds import bundle as host_bundle
from data.store.tests.test_seed_bundle import (
    CONTRACT_CASES,
    OTHER_REVISION,
    REPEATED_KEY_CASES,
    REPEATED_KEY_MESSAGE,
    REVISION,
    SENTINEL,
)
from tools.agent_mcp import seeds
from tools.agent_mcp.tests.harness import REPO_ROOT, tree_digest


pytestmark = pytest.mark.build_infra

WORKTREE = 'wt-one'
SEEDS_DIR = 'agent_mcp_seeds'
# Spelled out, not read from seeds.py: the oracle is the contract text in data/store/seeds/bundle.py.
EXPECTED_TAG = 'trader_joe-seed/1'
EXPECTED_KEYS = {'bundle', 'revision', 'sql', 'manifest'}
EXPECTED_CAP = 8 * 1024 * 1024

# Non-ASCII on purpose: the sizes the response carries are bytes, not characters.
SQL = f"INSERT INTO public.store_dataset_entry (id, note) VALUES ('{SENTINEL}', 'é');\n"
MANIFEST = json.dumps({'revision': REVISION, 'row_counts': {'store_dataset_entry': 1}, 'note': SENTINEL}) + '\n'
BUNDLE = seeds.SeedBundle(REVISION, SQL, MANIFEST)
VICTIM_BYTES = b'the victim, never written\n'


# --- V2: one table, two readers -------------------------------------------------------------------


def _host(stdout: str) -> tuple[str, str, str]:
    parsed = host_bundle.parse_bundle(stdout)
    return parsed.revision, parsed.sql, parsed.manifest


def _mcp(stdout: str) -> tuple[str, str, str]:
    parsed = seeds.parse_bundle(stdout.encode('utf-8'))
    return parsed.revision, parsed.sql, parsed.manifest


READERS = {'host': (_host, host_bundle.BundleRefused), 'mcp': (_mcp, seeds.BundleRefused)}
CASE_IDS = [case for case, _, _ in CONTRACT_CASES]


def test_the_contract_constants_agree_with_the_host_reader_and_the_contract_text():
    """BUNDLE_TAG, BUNDLE_KEYS, REVISION_PATTERN and MAX_BUNDLE_BYTES: one value each, in both readers."""
    assert seeds.BUNDLE_TAG == host_bundle.BUNDLE_TAG == EXPECTED_TAG
    assert set(seeds.BUNDLE_KEYS) == set(host_bundle.BUNDLE_KEYS) == EXPECTED_KEYS
    assert seeds.REVISION_PATTERN.pattern == host_bundle.REVISION_PATTERN.pattern == r'^[0-9a-f]{12}$'
    assert seeds.MAX_BUNDLE_BYTES == host_bundle.MAX_BUNDLE_BYTES == EXPECTED_CAP
    assert seeds.SEEDS_DIR_NAME == SEEDS_DIR


def test_the_table_carries_the_repeated_key_cases():
    """S11 / tj-irhy0a.23: the six repeated-key cases are part of what V2 runs, not a separate list."""
    ids = set(CASE_IDS)
    assert len(REPEATED_KEY_CASES) == 6 and {case for case, _, _ in REPEATED_KEY_CASES} <= ids
    assert sum(files is not None for _, _, files in CONTRACT_CASES) >= 5
    assert sum(files is None for _, _, files in CONTRACT_CASES) >= 30


@pytest.mark.parametrize('reader', sorted(READERS))
@pytest.mark.parametrize(('case', 'stdout', 'files'), CONTRACT_CASES, ids=CASE_IDS)
def test_both_readers_satisfy_the_contract_table(reader: str, case: str, stdout: str, files: dict[str, str] | None):
    """V2: accepted -> exactly the listed files' names and text; refused -> refused, naming no content."""
    parse, refused = READERS[reader]
    if files is None:
        with pytest.raises(refused) as raised:
            parse(stdout)
        message = str(raised.value)
        assert SENTINEL not in message and REVISION not in message and OTHER_REVISION not in message, message
    else:
        revision, sql, manifest = parse(stdout)
        assert {f'{revision}.sql': sql, f'{revision}.json': manifest} == files


@pytest.mark.parametrize(('case', 'stdout', 'files'), [c for c in CONTRACT_CASES if c[2] is not None], ids=str)
def test_the_mcp_writes_exactly_the_files_an_accepted_case_lists(tmp_path: Path, case: str, stdout: str, files):
    """V2, the writing half: the bytes on disk are the contract's, verbatim, and nothing else is there."""
    share = _share(tmp_path)
    seeds.write_seed(share, WORKTREE, seeds.parse_bundle(stdout.encode('utf-8')))
    directory = share / SEEDS_DIR / WORKTREE
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == {
        name: text.encode('utf-8') for name, text in files.items()
    }


@pytest.mark.parametrize(('case', 'stdout'), [(case, stdout) for case, stdout, _ in REPEATED_KEY_CASES], ids=str)
def test_the_mcp_refuses_a_repeated_key_for_that_reason(case: str, stdout: str):
    """S10.3 N1: the repeat itself is the refusal, as in the host reader -- not a later check it trips."""
    with pytest.raises(seeds.BundleRefused) as raised:
        seeds.parse_bundle(stdout.encode('utf-8'))
    assert str(raised.value) == REPEATED_KEY_MESSAGE


def test_the_mcp_refuses_bytes_that_are_not_utf8_without_echoing_them():
    """The MCP reads bytes; the host reads text. Undecodable stdout is refused, not replaced."""
    line = json.dumps({'bundle': EXPECTED_TAG, 'revision': REVISION, 'sql': SQL, 'manifest': MANIFEST})
    with pytest.raises(seeds.BundleRefused) as raised:
        seeds.parse_bundle(line.encode('utf-8') + b'\n\xff\xfe' + SENTINEL.encode() + b'\n')
    assert SENTINEL not in str(raised.value)


def test_the_cap_is_checked_before_decoding():
    """Over the cap is refused as over the cap even when it would not decode: nothing is parsed first."""
    with pytest.raises(seeds.BundleRefused) as raised:
        seeds.parse_bundle(b'\xff' * (EXPECTED_CAP + 1))
    assert 'cap' in str(raised.value)


def test_a_bundle_the_host_renders_is_accepted_by_the_mcp_unchanged():
    """The producer prints render_bundle's line; the MCP reads it back to the same three strings."""
    line = host_bundle.render_bundle(host_bundle.Bundle(REVISION, SQL, MANIFEST)) + '\n'
    assert _mcp(line) == (REVISION, SQL, MANIFEST)


# --- manifest_row_counts ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('manifest', 'expected'),
    [
        (
            {'revision': REVISION, 'row_counts': {'store_dataset_entry': 3, 'stock_market_activity': 0}},
            {'store_dataset_entry': 3, 'stock_market_activity': 0},
        ),
        ({'revision': REVISION}, None),
        ({'row_counts': []}, None),
        ({'row_counts': {'store_dataset_entry': -1}}, None),
        ({'row_counts': {'store_dataset_entry': True}}, None),
        ({'row_counts': {'store_dataset_entry': 1.0}}, None),
        ({'row_counts': {'store_dataset_entry': '1'}}, None),
        ({'row_counts': {SENTINEL: 1}}, None),
        ({'row_counts': {'public.store_dataset_entry': 1}}, None),
        ({'row_counts': {'a' * 64: 1}}, None),
    ],
    ids=[
        'counts',
        'absent',
        'a-list',
        'negative',
        'a-bool',
        'a-float',
        'a-string',
        'an-upper-case-name',
        'a-dotted-name',
        'a-name-over-63',
    ],
)
def test_manifest_row_counts_passes_only_table_names_and_non_negative_ints(manifest: dict, expected):
    """Only counts reach the response: anything the producer put there beyond a count makes it None."""
    assert seeds.manifest_row_counts(json.dumps(manifest) + '\n') == expected


@pytest.mark.parametrize('manifest', ['not json\n', '[1, 2]\n', '"text"\n'])
def test_manifest_row_counts_of_a_manifest_that_is_not_an_object_is_none(manifest: str):
    assert seeds.manifest_row_counts(manifest) is None


# --- V3: the writer -------------------------------------------------------------------------------


def _share(tmp_path: Path) -> Path:
    share = Path(os.path.realpath(tmp_path)) / 'share'
    share.mkdir(mode=0o700)
    return share


@pytest.fixture
def victim(tmp_path: Path) -> Path:
    """A directory outside the share holding one file, which nothing may ever write."""
    directory = Path(os.path.realpath(tmp_path)) / 'victim'
    directory.mkdir()
    (directory / 'secret').write_bytes(VICTIM_BYTES)
    return directory


def _outside_seeds(root: Path, share: Path) -> dict[str, str]:
    """tree_digest of everything under ROOT except <share>/agent_mcp_seeds and below."""
    seeds_dir = os.path.relpath(share / SEEDS_DIR, root)
    return {
        path: digest
        for path, digest in tree_digest(root).items()
        if path != seeds_dir and not path.startswith(seeds_dir + os.sep)
    }


def _temporaries(directory: Path) -> list[str]:
    return [name for name in os.listdir(directory) if name.endswith('.tmp')] if directory.is_dir() else []


def test_write_seed_writes_both_files_and_answers_relative_paths_sizes_and_counts(tmp_path: Path):
    share = _share(tmp_path)
    previous = os.umask(0o022)
    try:
        files = seeds.write_seed(share, WORKTREE, BUNDLE)
    finally:
        os.umask(previous)
    directory = share / SEEDS_DIR / WORKTREE
    assert files == seeds.SeedFiles(
        sql=f'{SEEDS_DIR}/{WORKTREE}/{REVISION}.sql',
        manifest=f'{SEEDS_DIR}/{WORKTREE}/{REVISION}.json',
        sql_bytes=len(SQL.encode('utf-8')),
        manifest_bytes=len(MANIFEST.encode('utf-8')),
        row_counts={'store_dataset_entry': 1},
    )
    assert files.sql_bytes == len(SQL) + 1, 'the size is in bytes: the e-acute is two'
    assert (share / files.sql).read_bytes() == SQL.encode('utf-8')
    assert (share / files.manifest).read_bytes() == MANIFEST.encode('utf-8')
    assert sorted(os.listdir(directory)) == [f'{REVISION}.json', f'{REVISION}.sql'], 'a temporary was left'
    for name in os.listdir(directory):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o644


def test_nothing_is_written_outside_the_seeds_directory(tmp_path: Path, victim: Path):
    root = Path(os.path.realpath(tmp_path))
    share = _share(tmp_path)
    (share / 'agent_mcp_token').write_text('token\n')
    before = _outside_seeds(root, share)
    seeds.write_seed(share, WORKTREE, BUNDLE)
    assert _outside_seeds(root, share) == before


def test_existing_seed_files_are_replaced_whole(tmp_path: Path):
    share = _share(tmp_path)
    directory = share / SEEDS_DIR / WORKTREE
    directory.mkdir(parents=True)
    (directory / f'{REVISION}.sql').write_text('old content, longer than the new one ' * 100)
    seeds.write_seed(share, WORKTREE, BUNDLE)
    assert (directory / f'{REVISION}.sql').read_bytes() == SQL.encode('utf-8')


@pytest.mark.parametrize('name', [f'{REVISION}.sql', f'{REVISION}.json'])
@pytest.mark.parametrize('dangling', [False, True], ids=['to-the-victim', 'dangling'])
def test_a_symlink_at_a_target_is_replaced_and_its_target_untouched(
    tmp_path: Path, victim: Path, name: str, dangling: bool
):
    """V3: the rename replaces the link itself; the file it named is byte-identical, a dangling one never made."""
    share = _share(tmp_path)
    directory = share / SEEDS_DIR / WORKTREE
    directory.mkdir(parents=True)
    pointed = victim / ('absent' if dangling else 'secret')
    (directory / name).symlink_to(pointed)
    before = tree_digest(victim)
    seeds.write_seed(share, WORKTREE, BUNDLE)
    assert not (directory / name).is_symlink() and (directory / name).is_file()
    assert tree_digest(victim) == before
    assert not (victim / 'absent').exists()


@pytest.mark.parametrize('name', [f'{REVISION}.sql', f'{REVISION}.json'])
def test_a_hard_link_at_a_target_is_replaced_and_its_inode_untouched(tmp_path: Path, victim: Path, name: str):
    """V3: never open an existing target for writing -- a hard link planted there is not written through."""
    share = _share(tmp_path)
    directory = share / SEEDS_DIR / WORKTREE
    directory.mkdir(parents=True)
    os.link(victim / 'secret', directory / name)
    seeds.write_seed(share, WORKTREE, BUNDLE)
    assert (victim / 'secret').read_bytes() == VICTIM_BYTES
    assert (victim / 'secret').stat().st_nlink == 1, 'the planted link still shares the victim inode'
    assert (directory / name).stat().st_ino != (victim / 'secret').stat().st_ino


def test_a_symlinked_share_root_is_refused(tmp_path: Path, victim: Path):
    """The walk starts from an O_NOFOLLOW fd of the share root itself."""
    link = Path(os.path.realpath(tmp_path)) / 'share_link'
    link.symlink_to(victim)
    before = tree_digest(victim)
    with pytest.raises(seeds.BundleRefused):
        seeds.write_seed(link, WORKTREE, BUNDLE)
    assert tree_digest(victim) == before


@pytest.mark.parametrize('component', ['seeds-dir', 'worktree-dir'])
def test_a_symlinked_directory_component_is_refused_and_its_target_untouched(
    tmp_path: Path, victim: Path, component: str
):
    """V3: a symlinked agent_mcp_seeds, or a symlinked worktree directory under it, is refused."""
    share = _share(tmp_path)
    if component == 'seeds-dir':
        (share / SEEDS_DIR).symlink_to(victim)
    else:
        (share / SEEDS_DIR).mkdir()
        (share / SEEDS_DIR / WORKTREE).symlink_to(victim)
    before = tree_digest(victim)
    with pytest.raises(seeds.BundleRefused) as raised:
        seeds.write_seed(share, WORKTREE, BUNDLE)
    assert SENTINEL not in str(raised.value)
    assert tree_digest(victim) == before


@pytest.mark.parametrize('component', ['seeds-dir', 'worktree-dir'])
def test_a_dangling_symlinked_component_is_refused_and_its_target_never_made(tmp_path: Path, component: str):
    share = _share(tmp_path)
    absent = Path(os.path.realpath(tmp_path)) / 'absent'
    if component == 'seeds-dir':
        (share / SEEDS_DIR).symlink_to(absent)
    else:
        (share / SEEDS_DIR).mkdir()
        (share / SEEDS_DIR / WORKTREE).symlink_to(absent)
    with pytest.raises(seeds.BundleRefused):
        seeds.write_seed(share, WORKTREE, BUNDLE)
    assert not absent.exists()


@pytest.mark.parametrize('component', ['seeds-dir', 'worktree-dir'])
def test_a_regular_file_in_the_path_is_refused_and_untouched(tmp_path: Path, component: str):
    share = _share(tmp_path)
    if component == 'seeds-dir':
        planted = share / SEEDS_DIR
    else:
        (share / SEEDS_DIR).mkdir()
        planted = share / SEEDS_DIR / WORKTREE
    planted.write_bytes(VICTIM_BYTES)
    with pytest.raises(seeds.BundleRefused):
        seeds.write_seed(share, WORKTREE, BUNDLE)
    assert planted.read_bytes() == VICTIM_BYTES


@pytest.mark.parametrize('name', [f'{REVISION}.sql', f'{REVISION}.json'])
def test_a_directory_at_a_target_is_refused_before_either_rename(tmp_path: Path, name: str):
    """The pair stays whole: neither target appears, and no temporary is left."""
    share = _share(tmp_path)
    directory = share / SEEDS_DIR / WORKTREE
    (directory / name).mkdir(parents=True)
    with pytest.raises(seeds.BundleRefused):
        seeds.write_seed(share, WORKTREE, BUNDLE)
    assert sorted(os.listdir(directory)) == [name]
    assert (directory / name).is_dir() and not list((directory / name).iterdir())


@pytest.mark.parametrize('name', ['', '.', '..', 'a/b', '../escape', 'a\0b'])
def test_a_worktree_name_that_is_not_one_component_is_refused_and_nothing_made(tmp_path: Path, name: str):
    share = _share(tmp_path)
    with pytest.raises(seeds.BundleRefused):
        seeds.write_seed(share, name, BUNDLE)
    assert os.listdir(share) == []


def test_a_failure_writing_the_second_file_leaves_neither_target_and_no_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Both temporaries are written before either rename: a failure at the second leaves nothing behind."""
    share = _share(tmp_path)
    real_fsync, calls = os.fsync, []

    def failing_fsync(fd: int) -> None:
        calls.append(fd)
        if len(calls) == 2:
            raise OSError(errno.ENOSPC, 'No space left on device')
        real_fsync(fd)

    monkeypatch.setattr(seeds.os, 'fsync', failing_fsync)
    with pytest.raises(OSError) as raised:
        seeds.write_seed(share, WORKTREE, BUNDLE)
    assert raised.value.errno == errno.ENOSPC
    directory = share / SEEDS_DIR / WORKTREE
    assert os.listdir(directory) == [], f'left behind: {os.listdir(directory)}'


def test_both_temporaries_exist_before_the_first_rename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    share = _share(tmp_path)
    directory = share / SEEDS_DIR / WORKTREE
    real_rename, seen = os.rename, []

    def watching_rename(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        seen.append(sorted(_temporaries(directory)))
        real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(seeds.os, 'rename', watching_rename)
    seeds.write_seed(share, WORKTREE, BUNDLE)
    assert len(seen) == 2 and len(seen[0]) == 2, seen


def test_seeds_imports_the_standard_library_only():
    """The MCP image carries tools/agent_mcp and nothing else (addendum 10 (1)): no data.store import."""
    tree = ast.parse((REPO_ROOT / 'tools' / 'agent_mcp' / 'seeds.py').read_text(encoding='utf-8'))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split('.')[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or '').split('.')[0])
    assert imported <= {
        'contextlib',
        'errno',
        'json',
        'os',
        're',
        'secrets',
        'stat',
        'collections',
        'dataclasses',
        'pathlib',
    }
