"""The seed bundle (data/store/seeds/bundle.py): the one stdout line, its parser, its writer, its contract.

WHY THIS FILE EXISTS (validator, tj-irhy0a.21; decision tj-vhboky.55 addendum S9 (3), ADR tj-4rr0la
addendum 10 (3)). The producer writes nothing; the bundle is the only thing that leaves it, and it is
read TWICE -- here (the host half of make seed-dump) and by the agent-stack MCP's own reader, which
cannot import this package. Whatever either reader accepts becomes a seed file, and the committed head
seed is what every later migration check reads, so every refusal must write nothing and the two
readers must agree.

THE CROSS-CONTRACT TABLE. CONTRACT_CASES below is the contract as data: (id, the producer's whole
stdout, the files a reader must write -- or None where it must refuse and write nothing). It holds
nothing but str and None, so tj-irhy0a.22's MCP reader test (its V2) runs the SAME table against
its own reader by importing it:

    from data.store.tests.test_seed_bundle import CONTRACT_CASES

and asserting, per case, that its reader accepts exactly the accepted cases and writes exactly the
listed bytes under exactly the listed names. This file runs the table against parse_bundle and
write_bundle (test_the_host_reader_satisfies_the_contract_table). A change to the contract changes
this table, and so both readers' tests.

WHAT TIER THIS IS. Pure: files go to tmp_path, and the repository's tests/ refusal is exercised
against a throw-away repository root, plus one read-only check against the real checkout.
"""

import ast
import errno
import json
import os
import stat
from pathlib import Path

import pytest

from common.tests.roots import SERVER_ROOT
from data.store.seeds import bundle as bundle_module
from data.store.seeds.bundle import (
    BUNDLE_KEYS,
    BUNDLE_TAG,
    MAX_BUNDLE_BYTES,
    REPO_ROOT,
    REVISION_PATTERN,
    Bundle,
    BundleRefused,
    check_out_dir,
    parse_bundle,
    render_bundle,
    write_bundle,
)
from data.store.seeds.manifest import render_manifest
from data.store.seeds.producer import VERSIONS_DIR


pytestmark = pytest.mark.data_store

REVISION = 'eec8f88a7443'
OTHER_REVISION = '8f41c2d7a3b9'
# A distinctive value inside the content, so an echo of the bundle into a message is found.
SENTINEL = 'CONTENTSENTINEL'
SQL = f"INSERT INTO public.store_dataset_entry (id, owner) VALUES ('{SENTINEL}', 'seed-owner-a');\n"


def _manifest(revision: str = REVISION, **extra) -> str:
    return render_manifest({'revision': revision, 'producer': 'data.store.seeds', 'note': SENTINEL, **extra})


MANIFEST = _manifest()
MISSING = object()


def bundle_line(**overrides) -> str:
    """One bundle line; an override of MISSING drops the key."""
    obj = {'bundle': BUNDLE_TAG, 'revision': REVISION, 'sql': SQL, 'manifest': MANIFEST}
    obj.update(overrides)
    return json.dumps({key: value for key, value in obj.items() if value is not MISSING})


def _padded_to(total: int) -> str:
    """A valid stdout (one bundle line and its newline) of exactly `total` UTF-8 bytes."""
    base = bundle_line(sql='\n') + '\n'
    return bundle_line(sql='A' * (total - len(base.encode('utf-8'))) + '\n') + '\n'


def _repeating(obj_text: str, member: str) -> str:
    """The JSON object text with one more member appended: '{..., <member>}'. Used to repeat a key."""
    assert obj_text.endswith('}')
    return f'{obj_text[:-1]}, {member}}}'


# Repeated keys (tj-irhy0a.23 N1). Each is refused whatever a last-wins OR a first-wins parser would make
# of it: the equal-values and escape-spelled cases are valid under both, 'invalid-then-valid' under
# last-wins, 'valid-then-invalid' under first-wins. The manifest cases are valid unless the manifest's
# own parse refuses the repeat, at any depth.
MANIFEST_REPEATING_REVISION = _repeating(MANIFEST.rstrip('\n'), f'"revision": "{REVISION}"') + '\n'
MANIFEST_REPEATING_NESTED = _repeating(MANIFEST.rstrip('\n'), f'"nested": {{"k": "{SENTINEL}", "k": "{SENTINEL}"}}')
MANIFEST_REPEATING_NESTED += '\n'
REPEATED_KEY_CASES: list[tuple[str, str, None]] = [
    ('repeated-key-equal-values', _repeating(bundle_line(), f'"revision": "{REVISION}"'), None),
    ('repeated-key-invalid-then-valid', '{"revision": "NOT-HEX", ' + bundle_line()[1:], None),
    ('repeated-key-valid-then-invalid', _repeating(bundle_line(), '"revision": "NOT-HEX"'), None),
    ('repeated-key-escape-spelled', _repeating(bundle_line(), f'"\\u0073ql": {json.dumps(SQL)}'), None),
    ('repeated-key-in-the-manifest', bundle_line(manifest=MANIFEST_REPEATING_REVISION), None),
    ('repeated-key-nested-in-the-manifest', bundle_line(manifest=MANIFEST_REPEATING_NESTED), None),
]
REPEATED_KEY_MESSAGE = 'a JSON object names a key more than once'

FILES = {f'{REVISION}.sql': SQL, f'{REVISION}.json': MANIFEST}
AT_CAP = _padded_to(MAX_BUNDLE_BYTES)
AT_CAP_FILES = {f'{REVISION}.sql': json.loads(AT_CAP)['sql'], f'{REVISION}.json': MANIFEST}
# Under the cap in characters, over it in UTF-8 bytes: the cap counts bytes (raw, not \\u-escaped, e-acute).
OVER_IN_BYTES_ONLY = json.dumps(json.loads(bundle_line(sql='é' * (MAX_BUNDLE_BYTES // 2) + '\n')), ensure_ascii=False)
OVER_IN_BYTES_ONLY += '\n'
assert len(OVER_IN_BYTES_ONLY) < MAX_BUNDLE_BYTES < len(OVER_IN_BYTES_ONLY.encode('utf-8'))

# (id, the producer's whole stdout, the files a reader writes -- None: refuse and write nothing).
CONTRACT_CASES: list[tuple[str, str, dict[str, str] | None]] = [
    # Accepted.
    ('one-line', bundle_line() + '\n', FILES),
    ('no-final-newline', bundle_line(), FILES),
    ('other-lines-before', 'seed produced\n{"bundle": "not this one"}\n' + bundle_line() + '\n', FILES),
    ('blank-lines-after', bundle_line() + '\n\n   \n', FILES),
    ('exactly-at-the-cap', AT_CAP, AT_CAP_FILES),
    # Refused: framing.
    ('empty', '', None),
    ('blank-only', '\n  \n', None),
    ('a-non-bundle-last-line', bundle_line() + '\nseed produced\n', None),
    ('last-line-not-an-object', bundle_line() + '\n[1, 2]\n', None),
    ('last-line-truncated', bundle_line()[:-5] + '\n', None),
    ('one-byte-over-the-cap', _padded_to(MAX_BUNDLE_BYTES + 1), None),
    ('over-the-cap-in-utf8-bytes', OVER_IN_BYTES_ONLY, None),
    # Refused: keys.
    ('extra-key', bundle_line(extra=SENTINEL), None),
    ('missing-bundle', bundle_line(bundle=MISSING), None),
    ('missing-revision', bundle_line(revision=MISSING), None),
    ('missing-sql', bundle_line(sql=MISSING), None),
    ('missing-manifest', bundle_line(manifest=MISSING), None),
    ('wrong-tag', bundle_line(bundle='trader_joe-seed/2'), None),
    ('tag-not-a-string', bundle_line(bundle=1), None),
    # Refused: the revision.
    ('revision-upper-case', bundle_line(revision=REVISION.upper()), None),
    ('revision-11-chars', bundle_line(revision=REVISION[:-1]), None),
    ('revision-13-chars', bundle_line(revision=REVISION + 'a'), None),
    ('revision-trailing-newline', bundle_line(revision=REVISION + '\n'), None),
    ('revision-path', bundle_line(revision='../../etc/pw'), None),
    ('revision-not-a-string', bundle_line(revision=123456789012), None),
    ('revision-differs-from-manifest', bundle_line(revision=OTHER_REVISION), None),
    ('manifest-without-revision', bundle_line(manifest=render_manifest({'producer': SENTINEL})), None),
    ('manifest-not-json', bundle_line(manifest=f'{SENTINEL}\n'), None),
    ('manifest-json-list', bundle_line(manifest=f'["{REVISION}"]\n'), None),
    # Refused: sql and manifest text.
    ('sql-without-final-newline', bundle_line(sql=SQL.rstrip('\n')), None),
    ('sql-two-final-newlines', bundle_line(sql=SQL + '\n'), None),
    ('sql-empty', bundle_line(sql=''), None),
    ('sql-newline-only', bundle_line(sql='\n'), None),
    ('sql-not-a-string', bundle_line(sql=[SQL]), None),
    ('manifest-without-final-newline', bundle_line(manifest=MANIFEST.rstrip('\n')), None),
    ('manifest-two-final-newlines', bundle_line(manifest=MANIFEST + '\n'), None),
    ('manifest-not-a-string', bundle_line(manifest={'revision': REVISION}), None),
    # Refused: a key named twice, at the top level or anywhere in the manifest (tj-irhy0a.23 N1).
    *REPEATED_KEY_CASES,
]


def _files_in(directory: Path) -> dict[str, str]:
    if not directory.exists():
        return {}
    return {path.name: path.read_bytes().decode('utf-8') for path in directory.iterdir()}


# ---------------------------------------------------------------------------------------------
# The contract table, against the host reader
# ---------------------------------------------------------------------------------------------


def test_the_contract_table_holds_only_plain_data():
    """The MCP's test imports it; it must not need anything from data.store to use."""
    ids = [case for case, _, _ in CONTRACT_CASES]
    assert len(ids) == len(set(ids))
    for case, stdout, files in CONTRACT_CASES:
        assert isinstance(case, str) and isinstance(stdout, str)
        assert files is None or all(isinstance(k, str) and isinstance(v, str) for k, v in files.items())
    assert sum(files is not None for _, _, files in CONTRACT_CASES) >= 5
    assert sum(files is None for _, _, files in CONTRACT_CASES) >= 30


@pytest.mark.parametrize(('case', 'stdout', 'files'), CONTRACT_CASES, ids=[case for case, _, _ in CONTRACT_CASES])
def test_the_host_reader_satisfies_the_contract_table(tmp_path, capsys, case, stdout, files):
    out = tmp_path / 'seeds'
    status = bundle_module.main(['--out', str(out)], stdin=stdout)
    captured = capsys.readouterr()
    if files is None:
        assert status == 3
        assert captured.out == ''
        assert captured.err.startswith('seed bundle refused: ')
        assert SENTINEL not in captured.err, 'a refusal carries no content of the bundle'
        assert not out.exists(), 'a refusal writes nothing, not even the directory'
    else:
        assert status == 0
        assert _files_in(out) == files


@pytest.mark.parametrize(
    ('case', 'stdout'),
    [(case, stdout) for case, stdout, files in CONTRACT_CASES if files is None],
    ids=[case for case, _, files in CONTRACT_CASES if files is None],
)
def test_every_refusal_is_a_bundle_refused_with_no_content(case, stdout):
    with pytest.raises(BundleRefused) as raised:
        parse_bundle(stdout)
    assert SENTINEL not in str(raised.value)
    assert REVISION not in str(raised.value) and OTHER_REVISION not in str(raised.value)


@pytest.mark.parametrize(
    ('case', 'stdout'),
    [(case, stdout) for case, stdout, _ in REPEATED_KEY_CASES],
    ids=[case for case, _, _ in REPEATED_KEY_CASES],
)
def test_a_repeated_key_is_refused_for_that_reason_naming_no_key(case, stdout):
    """The refusal is the repeat itself, not a later check a last-wins parse happened to trip."""
    for line in (stdout, stdout + '\n'):
        with pytest.raises(BundleRefused) as raised:
            parse_bundle(line)
        assert str(raised.value) == REPEATED_KEY_MESSAGE


def test_the_repeated_key_cases_parse_under_last_wins_json():
    """Without the rule each would be read somehow: the cases test the rule, not broken JSON."""
    for _, stdout, _ in REPEATED_KEY_CASES:
        assert isinstance(json.loads(stdout), dict)


@pytest.mark.parametrize(
    'manifest', [MANIFEST_REPEATING_REVISION, MANIFEST_REPEATING_NESTED], ids=['revision', 'nested']
)
def test_render_and_write_refuse_a_manifest_with_a_repeated_key(tmp_path, manifest):
    """The producer's side and the writer's own check run the same manifest parse."""
    with pytest.raises(BundleRefused, match=f'^{REPEATED_KEY_MESSAGE}$'):
        render_bundle(Bundle(REVISION, SQL, manifest))
    with pytest.raises(BundleRefused, match=f'^{REPEATED_KEY_MESSAGE}$'):
        write_bundle(Bundle(REVISION, SQL, manifest), tmp_path / 'seeds')
    assert not (tmp_path / 'seeds').exists()


# ---------------------------------------------------------------------------------------------
# The round trip and the cap
# ---------------------------------------------------------------------------------------------


def test_render_parse_write_round_trips_byte_for_byte(tmp_path):
    seed = Bundle(REVISION, SQL, MANIFEST)
    line = render_bundle(seed)
    assert '\n' not in line
    parsed = parse_bundle(line + '\n')
    assert parsed == seed
    sql_path, manifest_path = write_bundle(parsed, tmp_path / 'a' / 'b')
    assert (sql_path, manifest_path) == (
        tmp_path / 'a' / 'b' / f'{REVISION}.sql',
        tmp_path / 'a' / 'b' / f'{REVISION}.json',
    )
    assert sql_path.read_bytes() == SQL.encode('utf-8')
    assert manifest_path.read_bytes() == MANIFEST.encode('utf-8')


def test_the_rendered_line_is_exactly_the_four_keys():
    assert json.loads(render_bundle(Bundle(REVISION, SQL, MANIFEST))) == {
        'bundle': 'trader_joe-seed/1',
        'revision': REVISION,
        'sql': SQL,
        'manifest': MANIFEST,
    }
    assert {'bundle', 'revision', 'sql', 'manifest'} == BUNDLE_KEYS


def test_newlines_and_carriage_returns_are_written_unchanged(tmp_path):
    sql = 'INSERT INTO t (a) VALUES (1);\r\nINSERT INTO t (a) VALUES (2);\n'
    sql_path, _ = write_bundle(parse_bundle(render_bundle(Bundle(REVISION, sql, MANIFEST))), tmp_path)
    assert sql_path.read_bytes() == sql.encode('utf-8')


def test_the_cap_is_eight_mib():
    assert MAX_BUNDLE_BYTES == 8 * 1024 * 1024


def test_render_accepts_a_line_at_the_cap_and_refuses_one_byte_more():
    at_cap = json.loads(_padded_to(MAX_BUNDLE_BYTES))
    render_bundle(Bundle(REVISION, at_cap['sql'], MANIFEST))
    over = json.loads(_padded_to(MAX_BUNDLE_BYTES + 1))
    with pytest.raises(BundleRefused, match=rf'^the bundle is over the {MAX_BUNDLE_BYTES}-byte cap$'):
        render_bundle(Bundle(REVISION, over['sql'], MANIFEST))


def test_the_cap_is_checked_before_parsing():
    """An over-cap stdout is refused for its size, whatever it holds."""
    with pytest.raises(BundleRefused, match=rf'^the output is over the {MAX_BUNDLE_BYTES}-byte cap$'):
        parse_bundle(_padded_to(MAX_BUNDLE_BYTES + 1))
    with pytest.raises(BundleRefused, match='over the'):
        parse_bundle('x' * (MAX_BUNDLE_BYTES + 1))


@pytest.mark.parametrize(
    'seed',
    [
        Bundle('NOT-HEX', SQL, MANIFEST),
        Bundle(REVISION, SQL.rstrip('\n'), MANIFEST),
        Bundle(REVISION, SQL, _manifest(OTHER_REVISION)),
    ],
    ids=['revision', 'sql-newline', 'manifest-revision'],
)
def test_render_refuses_what_parse_would_refuse(seed):
    with pytest.raises(BundleRefused):
        render_bundle(seed)


def test_every_revision_in_the_chain_passes_the_format_check():
    """The 12-hex check must never refuse a real revision (alembic's default ids are 12 hex).

    VERSIONS_DIR, not REPO_ROOT / 'data' / ... (tj-2bsw0k): the versions directory sits under the
    SERVER root, which REPO_ROOT does not name (it is the TRUE repository root -- see bundle.py).
    Importing producer's VERSIONS_DIR keeps this test from recomputing the server-root path itself,
    the same one-name-two-roots shape site 1 of tj-2bsw0k fixed in producer.py.
    """
    versions = VERSIONS_DIR
    revisions = []
    for path in sorted(versions.glob('*.py')):
        for node in ast.parse(path.read_text(encoding='utf-8')).body:
            if (isinstance(node, ast.Assign) and any(getattr(t, 'id', None) == 'revision' for t in node.targets)) or (
                isinstance(node, ast.AnnAssign) and getattr(node.target, 'id', None) == 'revision'
            ):
                revisions.append(ast.literal_eval(node.value))
    assert revisions, 'no revisions found'
    assert [revision for revision in revisions if not REVISION_PATTERN.fullmatch(revision)] == []


# ---------------------------------------------------------------------------------------------
# The output directory (moved from the producer: check_out_dir)
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / 'repo'
    (root / 'tests' / 'system' / 'seeds').mkdir(parents=True)
    (root / 'tests' / 'fakes').mkdir()
    (root / 'data' / 'store' / 'tests').mkdir(parents=True)
    return root


@pytest.mark.parametrize(
    'relative', ['tests', 'tests/system/seeds', 'tests/system/seeds/../../fakes', 'data/../tests/new', 'tests/']
)
def test_an_output_directory_under_tests_is_refused(repo, relative):
    with pytest.raises(BundleRefused, match='--allow-tests-dir'):
        check_out_dir(repo / relative, repo_root=repo)


def test_a_symlink_into_tests_is_refused(repo, tmp_path):
    link = tmp_path / 'looks-harmless'
    link.symlink_to(repo / 'tests')
    with pytest.raises(BundleRefused):
        check_out_dir(link / 'system' / 'seeds', repo_root=repo)


def test_a_symlinked_parent_resolving_into_tests_is_refused(repo, tmp_path):
    (tmp_path / 'outer').mkdir()
    (tmp_path / 'outer' / 'seeds').symlink_to(repo / 'tests' / 'system' / 'seeds')
    with pytest.raises(BundleRefused):
        check_out_dir(tmp_path / 'outer' / 'seeds' / 'new', repo_root=repo)


def test_tests_reached_through_its_own_real_path_is_refused(tmp_path):
    """When the repository's tests/ is itself a link, its target is tests/ too."""
    root = tmp_path / 'repo'
    root.mkdir()
    real = tmp_path / 'elsewhere' / 'real-tests'
    real.mkdir(parents=True)
    (root / 'tests').symlink_to(real)
    with pytest.raises(BundleRefused):
        check_out_dir(real / 'system', repo_root=root)


def test_allow_tests_dir_permits_it(repo):
    target = repo / 'tests' / 'system' / 'seeds'
    assert check_out_dir(target, allow_tests_dir=True, repo_root=repo) == target.resolve()


@pytest.mark.parametrize('relative', ['.', 'seed-out', 'data/store/tests', 'tests_elsewhere', 'testsX/y'])
def test_other_directories_are_permitted(repo, relative):
    """data/store/tests is a test directory, but not THE tests/ the seeds are committed to."""
    assert check_out_dir(repo / relative, repo_root=repo) == (repo / relative).resolve()


def test_the_real_checkouts_tests_is_refused_by_default():
    with pytest.raises(BundleRefused):
        check_out_dir(REPO_ROOT / 'tests' / 'system' / 'seeds')


def test_the_repo_root_is_the_checkout():
    """The TRUE repository root, named by what only it carries.

    `data/store/seeds/bundle.py` alone cannot be the discriminator: once the service trees move
    under server/ (epic tj-iontkq) that path exists beneath BOTH roots, so this assertion would
    hold just as well for the server root the old `parents[3]` count returned. pytest.ini and
    tests/ are what stay at the top of the repository, and tests/ is the directory the whole
    refusal below is about.
    """
    # The move this docstring anticipated has happened (tj-iontkq.4): bundle.py is under the SERVER
    # root now, and pytest.ini and tests/ below are the discriminators that stayed behind.
    assert (SERVER_ROOT / 'data' / 'store' / 'seeds' / 'bundle.py').is_file()
    assert (REPO_ROOT / 'pytest.ini').is_file()
    assert (REPO_ROOT / 'tests').is_dir()


# ---------------------------------------------------------------------------------------------
# REPO_ROOT's own resolution (tj-qanatv): the marker search behind check_out_dir's default.
#
# Every test above either passes repo_root= explicitly or runs the default against TODAY's
# checkout, where the true repository root and the server root are the same directory. None of
# them can therefore tell a marker search from the `Path(__file__).resolve().parents[3]` count it
# replaced -- measured, not assumed: with that count restored, and separately with the raise below
# replaced by a silent filesystem-root fallback, the whole data/store suite stayed green at 1172
# passed. The fabricated layouts here are the only shape in which either property is observable.


def _post_move_tree(tmp_path: Path) -> Path:
    """The shape epic tj-iontkq produces: the service trees one level down, tests/ left at the top."""
    root = tmp_path / 'repo'
    (root / 'tests' / 'system').mkdir(parents=True)
    (root / 'pytest.ini').write_text('[pytest]\n', encoding='utf-8')
    (root / 'server' / 'data' / 'store' / 'seeds').mkdir(parents=True)
    return root


def test_the_repo_root_is_found_by_marker_not_by_counting(tmp_path):
    """Post-move the fixed count lands on server/; the marker search lands on the real root."""
    root = _post_move_tree(tmp_path)
    seeds = root / 'server' / 'data' / 'store' / 'seeds'
    assert bundle_module._find_repo_root(seeds) == root
    # The count this replaced, spelled out so the difference is visible rather than argued: it
    # returns server/, and server/tests does not exist, which is why the refusal stopped firing.
    assert seeds.parents[2] == root / 'server'
    assert not (root / 'server' / 'tests').exists()


def test_the_marker_search_keeps_the_refusal_firing_after_the_move(tmp_path):
    """The property the search exists for, and the fail-open behaviour it replaces, side by side."""
    root = _post_move_tree(tmp_path)
    target = root / 'tests' / 'system' / 'seeds'
    found = bundle_module._find_repo_root(root / 'server' / 'data' / 'store' / 'seeds')
    with pytest.raises(BundleRefused, match='--allow-tests-dir'):
        check_out_dir(target, repo_root=found)
    # The old root, post-move: the same path through the same guard, permitted and silent. This
    # is the fail-OPEN direction tj-qanatv was filed for, pinned so it cannot come back unnoticed.
    assert check_out_dir(target, repo_root=root / 'server') == target.resolve()


def test_a_tree_with_no_marker_raises_rather_than_falling_back(tmp_path):
    """No fallback: a silent filesystem-root default is the same vacuous pass in a new place."""
    orphan = tmp_path / 'no-marker' / 'data' / 'store' / 'seeds'
    orphan.mkdir(parents=True)
    with pytest.raises(RuntimeError, match=r'pytest\.ini'):
        bundle_module._find_repo_root(orphan)


def test_a_refused_directory_is_never_created(repo):
    target = repo / 'tests' / 'system' / 'seeds' / 'new'
    with pytest.raises(BundleRefused):
        write_bundle(Bundle(REVISION, SQL, MANIFEST), target, repo_root=repo)
    assert not target.exists()
    assert list((repo / 'tests' / 'system' / 'seeds').iterdir()) == []


def test_an_invalid_bundle_writes_nothing_even_to_an_allowed_directory(tmp_path):
    target = tmp_path / 'seeds'
    with pytest.raises(BundleRefused):
        write_bundle(Bundle(REVISION, SQL, _manifest(OTHER_REVISION)), target)
    assert not target.exists()


def test_allow_tests_dir_writes_there(repo):
    target = repo / 'tests' / 'system' / 'seeds'
    write_bundle(Bundle(REVISION, SQL, MANIFEST), target, allow_tests_dir=True, repo_root=repo)
    assert _files_in(target) == FILES


def test_the_tests_refusal_also_reads_the_path_as_given(repo, tmp_path):
    """tests/<link> resolving OUTSIDE tests/ is still refused: the path as given names tests/."""
    outside = tmp_path / 'outside'
    outside.mkdir()
    (repo / 'tests' / 'escape').symlink_to(outside)
    with pytest.raises(BundleRefused, match='--allow-tests-dir'):
        check_out_dir(repo / 'tests' / 'escape', repo_root=repo)
    with pytest.raises(BundleRefused):
        write_bundle(Bundle(REVISION, SQL, MANIFEST), repo / 'tests' / 'escape', repo_root=repo)
    assert list(outside.iterdir()) == []


# ---------------------------------------------------------------------------------------------
# The no-follow, all-or-nothing writer (tj-irhy0a.23 N2; ADR tj-4rr0la addendum 10 (3))
#
# write_bundle runs on the host as the user, in a directory an agent can modify. Every case below
# plants something an agent could plant and checks that nothing outside tmp_path's own directories is
# written, and that each link's target is byte-identical afterwards.
# ---------------------------------------------------------------------------------------------


VICTIM = 'VICTIM CONTENT, MUST SURVIVE\n'
SEED = Bundle(REVISION, SQL, MANIFEST)
COMPONENT_REFUSAL = '^an output directory component is a symlink or not a directory$'


@pytest.fixture
def victim(tmp_path) -> Path:
    """A directory outside the output tree, holding one file, that no write may reach."""
    directory = tmp_path / 'victim'
    directory.mkdir()
    (directory / 'file').write_text(VICTIM, encoding='utf-8')
    return directory


def _snapshot(directory: Path) -> dict[str, bytes | str]:
    """Every entry under directory, recursively: bytes for a file, 'link -> target' for a link."""
    found: dict[str, bytes | str] = {}
    for path in sorted(directory.rglob('*')):
        key = str(path.relative_to(directory))
        if path.is_symlink():
            found[key] = f'link -> {path.readlink()}'
        elif path.is_file():
            found[key] = path.read_bytes()
        else:
            found[key] = 'dir'
    return found


def _temporaries(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir() if path.name.endswith('.tmp'))


def test_a_symlink_at_the_output_directory_is_refused(tmp_path, victim):
    before = _snapshot(victim)
    (tmp_path / 'out').symlink_to(victim)
    with pytest.raises(BundleRefused, match=COMPONENT_REFUSAL):
        write_bundle(SEED, tmp_path / 'out')
    assert _snapshot(victim) == before
    assert (tmp_path / 'out').is_symlink()


def test_a_symlink_at_an_intermediate_component_is_refused(tmp_path, victim):
    before = _snapshot(victim)
    (tmp_path / 'a').symlink_to(victim)
    with pytest.raises(BundleRefused, match=COMPONENT_REFUSAL):
        write_bundle(SEED, tmp_path / 'a' / 'b' / 'c')
    assert _snapshot(victim) == before, 'no component is made through the link'


def test_a_dangling_symlink_at_a_component_is_refused_and_its_target_never_made(tmp_path):
    (tmp_path / 'out').symlink_to(tmp_path / 'not-yet')
    with pytest.raises(BundleRefused, match=COMPONENT_REFUSAL):
        write_bundle(SEED, tmp_path / 'out')
    assert not (tmp_path / 'not-yet').exists()


def test_a_file_where_a_directory_belongs_is_refused_and_untouched(tmp_path):
    (tmp_path / 'a').write_text(VICTIM, encoding='utf-8')
    with pytest.raises(BundleRefused, match=COMPONENT_REFUSAL):
        write_bundle(SEED, tmp_path / 'a' / 'b')
    assert (tmp_path / 'a').read_text(encoding='utf-8') == VICTIM


@pytest.mark.parametrize('name', [f'{REVISION}.sql', f'{REVISION}.json'])
@pytest.mark.parametrize('dangling', [False, True], ids=['to-a-file', 'dangling'])
def test_a_symlink_at_a_target_name_is_replaced_and_its_target_untouched(tmp_path, victim, name, dangling):
    out = tmp_path / 'out'
    out.mkdir()
    target = victim / ('absent' if dangling else 'file')
    (out / name).symlink_to(target)
    before = _snapshot(victim)
    write_bundle(SEED, out)
    assert _snapshot(victim) == before, 'nothing written through the link, nothing created at its target'
    assert not (out / name).is_symlink()
    assert _files_in(out) == FILES
    assert _temporaries(out) == []


@pytest.mark.parametrize('name', [f'{REVISION}.sql', f'{REVISION}.json'])
def test_a_hard_link_at_a_target_name_is_replaced_and_its_inode_untouched(tmp_path, victim, name):
    out = tmp_path / 'out'
    out.mkdir()
    (out / name).hardlink_to(victim / 'file')
    write_bundle(SEED, out)
    assert (victim / 'file').read_text(encoding='utf-8') == VICTIM
    assert (victim / 'file').stat().st_nlink == 1, 'the target name is a new inode, the link is gone'
    assert _files_in(out) == FILES


def test_existing_seed_files_are_replaced_whole(tmp_path):
    out = tmp_path / 'out'
    out.mkdir()
    (out / f'{REVISION}.sql').write_text('OLD SQL, LONGER THAN THE NEW ONE ' * 20, encoding='utf-8')
    (out / f'{REVISION}.json').write_text('OLD JSON', encoding='utf-8')
    write_bundle(SEED, out)
    assert _files_in(out) == FILES


def test_the_files_are_created_0644_under_the_umask_and_no_temporary_remains(tmp_path):
    out = tmp_path / 'out'
    write_bundle(SEED, out)
    umask = os.umask(0)
    os.umask(umask)
    for name in FILES:
        assert stat.S_IMODE((out / name).stat().st_mode) == 0o644 & ~umask
    assert _temporaries(out) == []


def test_a_directory_swapped_for_a_symlink_mid_write_does_not_redirect_the_files(tmp_path, victim, monkeypatch):
    """The walk holds the directory by fd: renaming it away and planting a link at its path changes nothing."""
    out = tmp_path / 'out'
    moved = tmp_path / 'moved'
    real_write_new = bundle_module._write_new
    calls = []

    def swap_then_write(dir_fd, prefix, text):
        if not calls:
            out.rename(moved)
            out.symlink_to(victim)
        calls.append(prefix)
        return real_write_new(dir_fd, prefix, text)

    monkeypatch.setattr(bundle_module, '_write_new', swap_then_write)
    before = _snapshot(victim)
    write_bundle(SEED, out)
    assert _snapshot(victim) == before
    assert _files_in(moved) == FILES


def test_both_temporaries_exist_before_the_first_rename(tmp_path, monkeypatch):
    out = tmp_path / 'out'
    real_rename = os.rename
    seen: list[list[str]] = []

    def recording_rename(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        seen.append(sorted(os.listdir(src_dir_fd)))
        return real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(bundle_module.os, 'rename', recording_rename)
    write_bundle(SEED, out)
    assert len(seen) == 2
    first = seen[0]
    assert len(first) == 2 and all(name.endswith('.tmp') for name in first), first
    assert any(f'{REVISION}.sql' in name for name in first) and any(f'{REVISION}.json' in name for name in first)


@pytest.mark.parametrize('where', ['open', 'fsync'])
def test_a_failure_writing_the_second_file_leaves_neither_target_and_no_temporary(tmp_path, monkeypatch, where):
    """Fault injection on the second file: the old pair is untouched, nothing renamed, nothing left over."""
    out = tmp_path / 'out'
    out.mkdir()
    (out / f'{REVISION}.sql').write_text('OLD SQL\n', encoding='utf-8')
    (out / f'{REVISION}.json').write_text('OLD JSON\n', encoding='utf-8')
    before = _snapshot(out)
    if where == 'open':
        real = bundle_module._write_new
        calls = []

        def failing(dir_fd, prefix, text):
            calls.append(prefix)
            if len(calls) == 2:
                raise OSError(errno.ENOSPC, 'No space left on device')
            return real(dir_fd, prefix, text)

        monkeypatch.setattr(bundle_module, '_write_new', failing)
    else:
        real_fsync = os.fsync
        fsyncs = []

        def failing_fsync(fd):
            fsyncs.append(fd)
            if len(fsyncs) == 2:
                raise OSError(errno.EIO, 'Input/output error')
            return real_fsync(fd)

        monkeypatch.setattr(bundle_module.os, 'fsync', failing_fsync)
    with pytest.raises(OSError):
        write_bundle(SEED, out)
    monkeypatch.undo()
    assert _snapshot(out) == before


def test_an_os_failure_exits_1_naming_the_class_and_errno_only(tmp_path, capsys, monkeypatch):
    def failing(dir_fd, prefix, text):
        raise PermissionError(errno.EACCES, f'Permission denied: {SENTINEL}')

    monkeypatch.setattr(bundle_module, '_write_new', failing)
    assert bundle_module.main(['--out', str(tmp_path / 'out')], stdin=bundle_line()) == 1
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == 'seed bundle write failed: PermissionError (EACCES)\n'


_EXCLUSIVE_CREATE = os.O_CREAT | os.O_EXCL


@pytest.mark.parametrize('denied', [1, 2], ids=['first-create', 'second-create'])
def test_an_unwritable_directory_exits_1_and_leaves_nothing(tmp_path, capsys, monkeypatch, denied):
    """The kernel's EACCES at the writer's own exclusive create, injected so it holds for every uid.

    Mode bits cannot make this test: CI runs as root, and root ignores a directory's 0555 (the
    create succeeds and main returns 0). So the refusal is injected at os.open, the call the writer
    makes for each O_CREAT|O_EXCL temporary, raising what a non-root create in a 0555 directory
    raises. 'first-create' is the unwritable directory itself; 'second-create' is a denial arriving
    after the .sql temporary exists, so 'leaves nothing' also covers the cleanup of a temporary.
    """
    out = tmp_path / 'out'
    out.mkdir()
    real_open = os.open
    creates: list[str] = []

    def refusing_open(path, flags, mode=0o777, *, dir_fd=None):
        if flags & _EXCLUSIVE_CREATE == _EXCLUSIVE_CREATE:
            creates.append(path)
            if len(creates) == denied:
                raise PermissionError(errno.EACCES, 'Permission denied')
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(bundle_module.os, 'open', refusing_open)
    exit_code = bundle_module.main(['--out', str(out)], stdin=bundle_line())
    monkeypatch.undo()
    assert len(creates) == denied, f'the writer made {len(creates)} exclusive creates; the denial was not reached'
    assert exit_code == 1
    assert capsys.readouterr().err == 'seed bundle write failed: PermissionError (EACCES)\n'
    assert list(out.iterdir()) == []


def test_a_symlinked_component_exits_3_through_the_command(tmp_path, victim, capsys):
    (tmp_path / 'out').symlink_to(victim)
    assert bundle_module.main(['--out', str(tmp_path / 'out')], stdin=bundle_line()) == 3
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err == 'seed bundle refused: an output directory component is a symlink or not a directory\n'
    assert _snapshot(victim) == {'file': VICTIM.encode('utf-8')}


# The walk's root: trusted and followed; everything below it no-follow.


def test_the_default_root_is_the_repository_when_the_output_is_inside_it(tmp_path):
    """A checkout reached through a symlink still works: the repository root is the trusted start."""
    real = tmp_path / 'real-repo'
    real.mkdir()
    (tmp_path / 'linked-repo').symlink_to(real)
    write_bundle(SEED, tmp_path / 'linked-repo' / 'output' / 'seeds', repo_root=tmp_path / 'linked-repo')
    assert _files_in(real / 'output' / 'seeds') == FILES


def test_below_the_repository_root_a_symlink_is_still_refused(tmp_path, victim):
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / 'output').symlink_to(victim)
    with pytest.raises(BundleRefused, match=COMPONENT_REFUSAL):
        write_bundle(SEED, repo / 'output' / 'seeds', repo_root=repo)
    assert _snapshot(victim) == {'file': VICTIM.encode('utf-8')}


def test_outside_the_repository_the_walk_starts_at_the_filesystem_root(tmp_path):
    """No trusted prefix outside the repository: a symlinked parent anywhere on the path is refused."""
    real = tmp_path / 'real'
    real.mkdir()
    (tmp_path / 'linked').symlink_to(real)
    with pytest.raises(BundleRefused, match=COMPONENT_REFUSAL):
        write_bundle(SEED, tmp_path / 'linked' / 'seeds', repo_root=tmp_path / 'repo')
    assert list(real.iterdir()) == []


def test_an_explicit_root_is_followed_and_must_contain_the_output(tmp_path):
    real = tmp_path / 'real'
    real.mkdir()
    (tmp_path / 'linked').symlink_to(real)
    write_bundle(SEED, tmp_path / 'linked' / 'seeds', root=tmp_path / 'linked')
    assert _files_in(real / 'seeds') == FILES
    with pytest.raises(BundleRefused, match=r'^the output directory is not under the walk root$'):
        write_bundle(SEED, tmp_path / 'elsewhere', root=tmp_path / 'linked')
    assert not (tmp_path / 'elsewhere').exists()


@pytest.mark.parametrize(
    'name', [pytest.param(f'{REVISION}.sql', id='sql'), pytest.param(f'{REVISION}.json', id='json')]
)
def test_a_directory_at_a_target_name_fails_with_nothing_renamed(tmp_path, name):
    """tj-irhy0a.23 C1: a directory at either target name is refused with nothing renamed.

    Both targets are lstat-ed before the first rename, so the old pair stays untouched and both
    temporaries are removed. Before ca37934 the [json] case renamed the .sql and then failed,
    leaving half a pair.
    """
    out = _obstacle_dir(tmp_path, name)
    before = _snapshot(out)
    with pytest.raises(BundleRefused) as raised:
        write_bundle(SEED, out)
    assert _snapshot(out) == before
    message = str(raised.value)
    assert message == 'an output target exists and is neither a file nor a symlink'
    assert str(tmp_path) not in message and VICTIM not in message


@pytest.mark.parametrize('name', [f'{REVISION}.sql', f'{REVISION}.json'], ids=['sql', 'json'])
def test_the_command_exits_3_on_a_directory_at_a_target_and_names_no_path(tmp_path, capsys, name):
    out = _obstacle_dir(tmp_path, name)
    before = _snapshot(out)
    assert bundle_module.main(['--out', str(out)], stdin=bundle_line()) == 3
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err.startswith('seed bundle refused: an output target exists')
    assert str(tmp_path) not in captured.err and VICTIM not in captured.err
    assert _snapshot(out) == before


def _obstacle_dir(tmp_path: Path, name: str) -> Path:
    """An output directory holding an old pair, with a directory (and a file in it) at name."""
    out = tmp_path / 'out'
    out.mkdir()
    (out / f'{REVISION}.sql').write_text('OLD SQL\n', encoding='utf-8')
    (out / f'{REVISION}.json').write_text('OLD JSON\n', encoding='utf-8')
    (out / name).unlink()
    (out / name).mkdir()
    (out / name / 'kept').write_text(VICTIM, encoding='utf-8')
    return out


# ---------------------------------------------------------------------------------------------
# The host command: python -m data.store.seeds.bundle --out DIR [--allow-tests-dir]
# ---------------------------------------------------------------------------------------------


def test_the_command_writes_the_files_and_says_where(tmp_path, capsys):
    out = tmp_path / 'seeds'
    assert bundle_module.main(['--out', str(out)], stdin=bundle_line() + '\n') == 0
    captured = capsys.readouterr()
    assert captured.out == f'wrote {out / f"{REVISION}.sql"} and {out / f"{REVISION}.json"}\n'
    assert _files_in(out) == FILES


def test_the_command_refuses_the_real_tests_dir_and_writes_nothing(capsys):
    target = REPO_ROOT / 'tests' / '.seed-bundle-refusal-probe'
    assert not target.exists()
    try:
        assert bundle_module.main(['--out', str(target)], stdin=bundle_line()) == 3
        assert capsys.readouterr().err.startswith('seed bundle refused: the output directory is under tests/')
        assert not target.exists()
    finally:
        # If the refusal regresses, this run must not leave a seed in the real tree.
        for path in sorted(target.glob('*')) if target.exists() else []:
            path.unlink()
        if target.exists():
            target.rmdir()


def test_the_command_permits_tests_with_the_flag(repo):
    target = repo / 'tests' / 'system' / 'seeds'
    assert bundle_module.main(['--out', str(target), '--allow-tests-dir'], stdin=bundle_line()) == 0
    assert _files_in(target) == FILES


def test_the_command_requires_out(capsys):
    with pytest.raises(SystemExit) as raised:
        bundle_module.main([], stdin=bundle_line())
    assert raised.value.code == 2


# ---------------------------------------------------------------------------------------------
# The module: its contract text and its imports
# ---------------------------------------------------------------------------------------------


def test_the_docstring_states_the_contract_the_mcp_reader_implements():
    doc = ' '.join(bundle_module.__doc__.split())
    for clause in (
        'MAX_BUNDLE_BYTES (8 MiB, counted in UTF-8 bytes over the whole of stdout, newline included)',
        'LAST non-empty stdout line',
        'EXACTLY the four keys "bundle", "revision", "sql" and "manifest"',
        f"'{BUNDLE_TAG}'",
        '^[0-9a-f]{12}$',
        'each ending in exactly one newline',
        'carries no content of the bundle',
        '<revision>.sql and <revision>.json',
        'write nothing at all on any refusal',
        'refuse an object that names any key more than once, at the top level or inside the manifest',
        'never last-wins',
        'tj-irhy0a.22',
    ):
        assert clause in doc, clause
    assert REVISION_PATTERN.pattern == '^[0-9a-f]{12}$'


def test_the_bundle_module_imports_the_standard_library_only():
    import sys

    tree = ast.parse(Path(bundle_module.__file__).read_text(encoding='utf-8'))
    modules = {
        alias.name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }
    modules |= {
        node.module.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }
    assert modules and modules <= set(sys.stdlib_module_names)
