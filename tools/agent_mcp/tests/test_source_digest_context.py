"""tools/source_digest.sh --context <dir>: a TRUSTED copy of the script digesting another checkout (tj-iee6sn.8).

Design checked against: the architect's 17:25 UTC note (D) on tj-iee6sn.8 and the 04:08 validator finding
it answers. The MCP image bakes the script and the service Dockerfile under /opt/agent_mcp/compose, where
there are no sources and no .dockerignore, so without the flag the baked copy cannot digest the checkout.
With it: the Dockerfile parsed is still the one beside the script (../Dockerfile, the TRUSTED copy), the
files hashed and the .dockerignore honoured are the context's, and a context .dockerignore that is
missing, a symlink or not a regular file is a hard error with nothing on stdout. Without the flag nothing
changes: server/common/tests/test_ci_invariants.py holds that path's pins, unchanged.

Every test runs the REAL script, copied into a trusted layout in a tmp directory the way the MCP image
lays it out (<trusted>/Dockerfile and <trusted>/tools/source_digest.sh), over the real checkout or over
a fake one.
"""

import os
import shutil
import subprocess  # nosec B404 -- fixed invocations of the repository's own script
from pathlib import Path

import pytest

from tools.agent_mcp.tests.harness import REPO_ROOT


pytestmark = pytest.mark.build_infra

SCRIPT = REPO_ROOT / 'tools' / 'source_digest.sh'
SERVICES = [('data', 'store'), ('data', 'ingest')]
HEX = frozenset('0123456789abcdef')
TIMEOUT = 120

# A fake checkout: the shape test_ci_invariants.py's sandbox uses, one service and one shared package.
FAKE_DOCKERFILE = """\
FROM scratch
COPY ./pkg /code/pkg
COPY ./entry.sh /code/entry.sh
COPY ./${SERVICE_PATH}/${SERVICE_NAME}/app /code/${SERVICE_PATH}/${SERVICE_NAME}/app
"""
FAKE_DOCKERIGNORE = 'pkg/tests/\n**/__pycache__/\n**/*.py[cod]\n'
FAKE_SERVICE = ('svc', 'demo')


def _run(script: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # nosec B603 -- argument list, no shell
        [str(script), *arguments], capture_output=True, text=True, check=False, timeout=TIMEOUT
    )


def _digest(script: Path, *arguments: str) -> str:
    result = _run(script, *arguments)
    assert result.returncode == 0, (arguments, result.returncode, result.stderr)
    assert result.stdout.endswith('\n') and len(result.stdout) == 65 and set(result.stdout[:-1]) <= HEX, result.stdout
    return result.stdout[:-1]


def _trusted(root: Path, dockerfile: str | None = None) -> Path:
    """<root>/Dockerfile and <root>/tools/source_digest.sh, the MCP image's layout; returns the script."""
    (root / 'tools').mkdir(parents=True)
    shutil.copy2(SCRIPT, root / 'tools' / 'source_digest.sh')
    if dockerfile is None:
        shutil.copyfile(REPO_ROOT / 'Dockerfile', root / 'Dockerfile')
    else:
        (root / 'Dockerfile').write_text(dockerfile, encoding='utf-8')
    return root / 'tools' / 'source_digest.sh'


def _fake_checkout(root: Path) -> Path:
    (root / 'pkg' / 'tests').mkdir(parents=True)
    (root / 'pkg' / '__init__.py').write_text('', encoding='utf-8')
    (root / 'pkg' / 'mod.py').write_text('VALUE = 1\n', encoding='utf-8')
    (root / 'pkg' / 'tests' / 'test_mod.py').write_text('assert True\n', encoding='utf-8')
    (root / 'svc' / 'demo' / 'app').mkdir(parents=True)
    (root / 'svc' / 'demo' / 'app' / 'main.py').write_text('print(1)\n', encoding='utf-8')
    (root / 'entry.sh').write_text('#! /bin/sh\n', encoding='utf-8')
    (root / 'entry.sh').chmod(0o755)
    (root / 'Dockerfile').write_text(FAKE_DOCKERFILE, encoding='utf-8')
    (root / '.dockerignore').write_text(FAKE_DOCKERIGNORE, encoding='utf-8')
    return root


@pytest.fixture
def fake(tmp_path: Path) -> tuple[Path, Path]:
    """(the trusted script over FAKE_DOCKERFILE, a fake checkout)."""
    return _trusted(tmp_path / 'trusted', FAKE_DOCKERFILE), _fake_checkout(tmp_path / 'checkout')


def _refused(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    assert result.stdout == '', f'a refusal printed {result.stdout!r} on stdout'
    assert result.stderr.strip(), 'a refusal says why, on stderr'


# --- (i) the trusted copy over the real checkout is make dev-build's digest -------------------------


@pytest.mark.parametrize(('service_path', 'service_name'), SERVICES, ids=['store', 'ingest'])
def test_the_trusted_copy_over_the_checkout_gives_the_in_tree_digest(tmp_path: Path, service_path, service_name):
    """The bead's acceptance, without docker: the digest make dev-build stamps equals build's."""
    trusted = _trusted(tmp_path / 'compose')
    in_tree = _digest(SCRIPT, service_path, service_name)
    assert _digest(trusted, '--context', str(REPO_ROOT), service_path, service_name) == in_tree
    for mode in ('--list-paths', '--list-files'):
        mine = _run(SCRIPT, mode, service_path, service_name)
        theirs = _run(trusted, '--context', str(REPO_ROOT), mode, service_path, service_name)
        assert mine.returncode == theirs.returncode == 0, (mine.stderr, theirs.stderr)
        assert theirs.stdout == mine.stdout and mine.stdout.strip(), mode


def test_the_two_services_get_different_digests_through_the_trusted_copy(tmp_path: Path):
    trusted = _trusted(tmp_path / 'compose')
    digests = {_digest(trusted, '--context', str(REPO_ROOT), *service) for service in SERVICES}
    assert len(digests) == 2, digests


def test_without_the_flag_the_trusted_copy_cannot_digest_the_checkout(tmp_path: Path):
    """Why the flag exists (04:08 finding): beside the baked copy there are no sources, so it fails loud."""
    trusted = _trusted(tmp_path / 'compose')
    result = _run(trusted, 'data', 'store')
    assert result.returncode == 4 and result.stdout == '', (result.returncode, result.stdout, result.stderr)


def test_a_symlinked_context_directory_digests_the_directory_it_names(fake, tmp_path: Path):
    """Realpath -e: the context is resolved once, so a link to the checkout digests the checkout."""
    trusted, checkout = fake
    link = tmp_path / 'link'
    link.symlink_to(checkout, target_is_directory=True)
    assert _digest(trusted, '--context', str(link), *FAKE_SERVICE) == _digest(
        trusted, '--context', str(checkout), *FAKE_SERVICE
    )


# --- (ii) the context's .dockerignore, and only that one, is honoured; anything but a regular file refuses


def test_a_context_without_a_dockerignore_refuses_with_no_digest(fake):
    trusted, checkout = fake
    (checkout / '.dockerignore').unlink()
    _refused(_run(trusted, '--context', str(checkout), *FAKE_SERVICE))


def test_a_symlinked_dockerignore_refuses_even_to_identical_rules(fake, tmp_path: Path):
    trusted, checkout = fake
    elsewhere = tmp_path / 'rules'
    elsewhere.write_text(FAKE_DOCKERIGNORE, encoding='utf-8')
    (checkout / '.dockerignore').unlink()
    (checkout / '.dockerignore').symlink_to(elsewhere)
    _refused(_run(trusted, '--context', str(checkout), *FAKE_SERVICE))


def test_a_dangling_symlinked_dockerignore_refuses(fake, tmp_path: Path):
    trusted, checkout = fake
    (checkout / '.dockerignore').unlink()
    (checkout / '.dockerignore').symlink_to(tmp_path / 'absent')
    _refused(_run(trusted, '--context', str(checkout), *FAKE_SERVICE))


def test_a_directory_dockerignore_refuses(fake):
    trusted, checkout = fake
    (checkout / '.dockerignore').unlink()
    (checkout / '.dockerignore').mkdir()
    _refused(_run(trusted, '--context', str(checkout), *FAKE_SERVICE))


def test_a_fifo_dockerignore_refuses_without_blocking(fake):
    """A FIFO with no writer would block a read forever; it is refused before anything opens it."""
    trusted, checkout = fake
    (checkout / '.dockerignore').unlink()
    os.mkfifo(checkout / '.dockerignore')
    _refused(_run(trusted, '--context', str(checkout), *FAKE_SERVICE))


@pytest.mark.parametrize('mode', ['--list-paths', '--list-files'])
def test_the_list_modes_refuse_a_missing_dockerignore_too(fake, mode: str):
    trusted, checkout = fake
    (checkout / '.dockerignore').unlink()
    _refused(_run(trusted, '--context', str(checkout), mode, *FAKE_SERVICE))


def test_the_contexts_dockerignore_is_honoured(fake):
    """A file under an excluded directory does not move the digest; the same file once the rule is gone does."""
    trusted, checkout = fake
    before = _digest(trusted, '--context', str(checkout), *FAKE_SERVICE)
    (checkout / 'pkg' / 'tests' / 'test_new.py').write_text('assert 1\n', encoding='utf-8')
    assert _digest(trusted, '--context', str(checkout), *FAKE_SERVICE) == before
    (checkout / '.dockerignore').write_text('**/__pycache__/\n**/*.py[cod]\n', encoding='utf-8')
    assert _digest(trusted, '--context', str(checkout), *FAKE_SERVICE) != before


def test_a_dockerignore_beside_the_trusted_copy_is_not_read(fake):
    """The rules are the context's: one in the trusted directory (where BuildKit does not look) changes nothing."""
    trusted, checkout = fake
    before = _digest(trusted, '--context', str(checkout), *FAKE_SERVICE)
    (trusted.parent.parent / '.dockerignore').write_text('pkg/\n', encoding='utf-8')
    assert _digest(trusted, '--context', str(checkout), *FAKE_SERVICE) == before


# --- usage: --context first, with a directory -------------------------------------------------------


@pytest.mark.parametrize(
    'arguments',
    [('--context',), ('--context', 'svc', 'demo'), ('--context', '', 'svc', 'demo')],
    ids=['no-directory', 'a-service-word-as-directory', 'empty-directory'],
)
def test_context_without_a_usable_directory_refuses(fake, arguments):
    trusted, _ = fake
    _refused(_run(trusted, *arguments))


def test_a_context_that_does_not_exist_refuses(fake, tmp_path: Path):
    trusted, _ = fake
    _refused(_run(trusted, '--context', str(tmp_path / 'absent'), *FAKE_SERVICE))


def test_a_context_that_is_a_file_refuses(fake):
    trusted, checkout = fake
    _refused(_run(trusted, '--context', str(checkout / 'entry.sh'), *FAKE_SERVICE))


def test_context_after_a_mode_flag_is_an_unknown_option(fake):
    """It must come first: anywhere else it is not the flag."""
    trusted, checkout = fake
    _refused(_run(trusted, '--list-paths', '--context', str(checkout), *FAKE_SERVICE))


@pytest.mark.parametrize('arguments', [(), ('svc',), ('svc', 'demo', 'extra')], ids=['none', 'one', 'three'])
def test_context_with_the_wrong_operand_count_refuses(fake, arguments):
    trusted, checkout = fake
    _refused(_run(trusted, '--context', str(checkout), *arguments))


# --- (iii) the TRUSTED Dockerfile is the one parsed -------------------------------------------------


def test_a_copy_added_only_to_the_contexts_dockerfile_does_not_change_coverage(fake):
    trusted, checkout = fake
    (checkout / 'extra.txt').write_text('extra\n', encoding='utf-8')
    before_paths = _run(trusted, '--context', str(checkout), '--list-paths', *FAKE_SERVICE).stdout
    before = _digest(trusted, '--context', str(checkout), *FAKE_SERVICE)
    with (checkout / 'Dockerfile').open('a', encoding='utf-8') as dockerfile:
        dockerfile.write('COPY ./extra.txt /code/extra.txt\n')
    assert _run(trusted, '--context', str(checkout), '--list-paths', *FAKE_SERVICE).stdout == before_paths
    assert 'extra.txt' not in before_paths
    assert _digest(trusted, '--context', str(checkout), *FAKE_SERVICE) == before


def test_the_same_copy_in_the_trusted_dockerfile_does_change_coverage(fake):
    """Non-vacuity for the test above: the line is one the parser reads."""
    trusted, checkout = fake
    (checkout / 'extra.txt').write_text('extra\n', encoding='utf-8')
    before = _digest(trusted, '--context', str(checkout), *FAKE_SERVICE)
    with (trusted.parent.parent / 'Dockerfile').open('a', encoding='utf-8') as dockerfile:
        dockerfile.write('COPY ./extra.txt /code/extra.txt\n')
    paths = _run(trusted, '--context', str(checkout), '--list-paths', *FAKE_SERVICE).stdout.split()
    assert 'extra.txt' in paths, paths
    assert _digest(trusted, '--context', str(checkout), *FAKE_SERVICE) != before


def test_a_broken_context_dockerfile_does_not_matter(fake):
    """A Dockerfile the parser would refuse, in the context only, is never read."""
    trusted, checkout = fake
    before = _digest(trusted, '--context', str(checkout), *FAKE_SERVICE)
    (checkout / 'Dockerfile').write_text('COPY ["a", "b"]\n', encoding='utf-8')
    assert _digest(trusted, '--context', str(checkout), *FAKE_SERVICE) == before


# --- a hostile checkout: the digest is still one line, or nothing ----------------------------------


def test_odd_but_legal_file_names_are_digested(fake):
    """Spaces, a leading dash and glob characters in a name: hashed by name, never expanded or parsed."""
    trusted, checkout = fake
    before = _digest(trusted, '--context', str(checkout), *FAKE_SERVICE)
    for name in ('with space.py', '-dash.py', 'star*.py', 'q?.py', '[br].py'):
        (checkout / 'pkg' / name).write_text(f'# {name}\n', encoding='utf-8')
    files = _run(trusted, '--context', str(checkout), '--list-files', *FAKE_SERVICE).stdout.splitlines()
    assert {'pkg/with space.py', 'pkg/-dash.py', 'pkg/star*.py', 'pkg/q?.py', 'pkg/[br].py'} <= set(files), files
    after = _digest(trusted, '--context', str(checkout), *FAKE_SERVICE)
    assert after != before and _digest(trusted, '--context', str(checkout), *FAKE_SERVICE) == after


def test_a_newline_in_a_file_name_refuses_with_no_digest(fake):
    trusted, checkout = fake
    (checkout / 'pkg' / 'two\nlines.py').write_text('x\n', encoding='utf-8')
    result = _run(trusted, '--context', str(checkout), *FAKE_SERVICE)
    assert result.returncode == 4 and result.stdout == '', (result.returncode, result.stdout, result.stderr)


def test_a_symlink_inside_a_copy_source_is_recorded_not_followed(fake, tmp_path: Path):
    """A link to a file outside the checkout: its target's CONTENT is never read, only the link text."""
    trusted, checkout = fake
    outside = tmp_path / 'outside.txt'
    outside.write_text('secret one\n', encoding='utf-8')
    (checkout / 'pkg' / 'link.py').symlink_to(outside)
    first = _digest(trusted, '--context', str(checkout), *FAKE_SERVICE)
    outside.write_text('secret two\n', encoding='utf-8')
    assert _digest(trusted, '--context', str(checkout), *FAKE_SERVICE) == first
    (checkout / 'pkg' / 'link.py').unlink()
    (checkout / 'pkg' / 'link.py').symlink_to(tmp_path / 'other.txt')
    assert _digest(trusted, '--context', str(checkout), *FAKE_SERVICE) != first


def test_a_fifo_inside_a_copy_source_is_not_read_and_does_not_block(fake):
    trusted, checkout = fake
    before = _digest(trusted, '--context', str(checkout), *FAKE_SERVICE)
    os.mkfifo(checkout / 'pkg' / 'pipe.py')
    assert _digest(trusted, '--context', str(checkout), *FAKE_SERVICE) == before


# --- a symlinked COPY-source path under --context (tj-iee6sn.15, B5b) -------------------------------
#
# Design checked against: tj-iee6sn.15's WHAT (1) and TESTS (i)-(iii), from ruling (1)(a) of the
# architect's 16:59 UTC gate note on tj-iee6sn.8 (tj-yb1bxj: a link at ANY component of a COPY source
# makes the trusted script hash files outside the checkout, and the stamp stops describing the image).
# Under --context a COPY source that is a link, or sits below one at any component, refuses: exit 4,
# empty stdout, the component named on stderr. Links INSIDE a directory source stay recorded. Without
# the flag nothing changes. The sources are the REAL Dockerfile's, through the real script.


def _real_copy_sources(service: tuple[str, str]) -> list[str]:
    result = _run(SCRIPT, '--list-paths', *service)
    assert result.returncode == 0, result.stderr
    return result.stdout.split()


REAL_SOURCES = {service: _real_copy_sources(service) for service in SERVICES}
# Which REAL COPY sources are single files; every other one is a directory. Spelled here, not read off
# the checkout: gen/proto/python is a directory the checkout only has once generated.
REAL_FILE_SOURCES = frozenset({'entrypoint.sh', 'pyproject.toml', 'uv.lock'})


def _prefixes(source: str) -> list[str]:
    parts = source.split('/')
    return ['/'.join(parts[: index + 1]) for index in range(len(parts))]


# Every (service, COPY source, component) the guard must refuse at: each source's leaf, and each
# intermediate directory on its way down. Deduplicated by component, so `server` is one case.
_LINK_CASES: dict[str, tuple[tuple[str, str], str, str]] = {}
for _service, _sources in REAL_SOURCES.items():
    for _source in _sources:
        for _component in _prefixes(_source):
            _LINK_CASES.setdefault(_component, (_service, _source, _component))


def _real_checkout(root: Path) -> Path:
    """Every COPY source of the REAL Dockerfile, for both services, with the real .dockerignore."""
    for source in sorted({source for sources in REAL_SOURCES.values() for source in sources}):
        target = root / source
        if source in REAL_FILE_SOURCES:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f'# {source}\n', encoding='utf-8')
        else:
            (target / 'sub').mkdir(parents=True, exist_ok=True)
            (target / 'mod.py').write_text(f'# {source}\n', encoding='utf-8')
            (target / 'sub' / 'inner.py').write_text(f'# {source} inner\n', encoding='utf-8')
    (root / 'entrypoint.sh').chmod(0o755)
    shutil.copyfile(REPO_ROOT / '.dockerignore', root / '.dockerignore')
    return root


def _link_out(checkout: Path, component: str, elsewhere: Path) -> Path:
    """Move CHECKOUT/COMPONENT to ELSEWHERE/COMPONENT, whole, and leave a symlink to it in its place."""
    moved = elsewhere / component
    moved.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(checkout / component, moved)
    (checkout / component).symlink_to(moved, target_is_directory=moved.is_dir())
    return moved


def _symlink_refused(result: subprocess.CompletedProcess, component: str) -> None:
    assert result.returncode == 4, (result.returncode, result.stdout, result.stderr)
    assert result.stdout == '', f'a refusal printed {result.stdout!r} on stdout'
    assert f"passes through a symlink at '{component}'; refusing to digest" in result.stderr, result.stderr


@pytest.fixture
def real(tmp_path: Path) -> tuple[Path, Path]:
    """(the trusted script over the REAL Dockerfile, a fake checkout holding every real COPY source)."""
    return _trusted(tmp_path / 'trusted'), _real_checkout(tmp_path / 'checkout')


def test_the_link_cases_cover_files_directories_and_intermediate_components():
    """Non-vacuity for the sweep: the real Dockerfile has file and directory sources, and nested ones."""
    sources = {source for sources in REAL_SOURCES.values() for source in sources}
    assert sources >= REAL_FILE_SOURCES, sources
    assert sources - REAL_FILE_SOURCES, 'no directory COPY source'
    intermediates = {component for _, source, component in _LINK_CASES.values() if component != source}
    assert {'server', 'server/data', 'server/data/store', 'server/data/ingest', 'gen', 'gen/proto'} <= intermediates


def test_the_fake_real_checkout_digests_under_the_flag(real):
    """Non-vacuity for every refusal below: with no link the same tree digests, for both services."""
    trusted, checkout = real
    for service in SERVICES:
        _digest(trusted, '--context', str(checkout), *service)


@pytest.mark.parametrize(('service', 'source', 'component'), list(_LINK_CASES.values()), ids=list(_LINK_CASES))
def test_a_symlink_at_any_component_of_a_real_copy_source_refuses(real, tmp_path: Path, service, source, component):
    """TESTS (i): a link at the leaf or at any directory above it, the target HAVING every child path."""
    trusted, checkout = real
    moved = _link_out(checkout, component, tmp_path / 'elsewhere')
    assert (checkout / source).exists(), 'the link must resolve: a refusal for a missing path proves nothing'
    _symlink_refused(_run(trusted, '--context', str(checkout), *service), component)
    _symlink_refused(_run(trusted, '--context', str(checkout), '--list-files', *service), component)
    assert moved.exists()


def test_a_dangling_symlink_at_a_copy_source_refuses_as_a_symlink(real, tmp_path: Path):
    trusted, checkout = real
    (checkout / 'uv.lock').unlink()
    (checkout / 'uv.lock').symlink_to(tmp_path / 'absent')
    _symlink_refused(_run(trusted, '--context', str(checkout), 'data', 'store'), 'uv.lock')


def test_a_missing_copy_source_still_refuses_as_missing(real):
    """A missing source keeps today's exit 4 and message; the symlink guard does not re-word it."""
    trusted, checkout = real
    (checkout / 'uv.lock').unlink()
    result = _run(trusted, '--context', str(checkout), 'data', 'store')
    assert result.returncode == 4 and result.stdout == '', (result.returncode, result.stdout, result.stderr)
    assert "COPY source 'uv.lock' does not exist" in result.stderr and 'symlink' not in result.stderr, result.stderr


def test_a_trailing_slash_copy_source_through_a_symlink_refuses(fake, tmp_path: Path):
    """`COPY ./pkg/ ...` names the same source: the parser drops the slash, and the guard still sees the link."""
    trusted, checkout = fake
    dockerfile = trusted.parent.parent / 'Dockerfile'
    dockerfile.write_text(FAKE_DOCKERFILE.replace('COPY ./pkg ', 'COPY ./pkg/ '), encoding='utf-8')
    assert 'COPY ./pkg/ ' in dockerfile.read_text(encoding='utf-8')
    _link_out(checkout, 'pkg', tmp_path / 'elsewhere')
    _symlink_refused(_run(trusted, '--context', str(checkout), *FAKE_SERVICE), 'pkg')


def test_a_leading_dash_copy_source_is_checked_as_a_path(fake, tmp_path: Path):
    """A source spelled `-dash` is tested as a file name, not taken for a test(1) option."""
    trusted, checkout = fake
    with (trusted.parent.parent / 'Dockerfile').open('a', encoding='utf-8') as dockerfile:
        dockerfile.write('COPY ./-dash /code/dash\n')
    (checkout / '-dash').write_text('dash\n', encoding='utf-8')
    _digest(trusted, '--context', str(checkout), *FAKE_SERVICE)
    _link_out(checkout, '-dash', tmp_path / 'elsewhere')
    _symlink_refused(_run(trusted, '--context', str(checkout), *FAKE_SERVICE), '-dash')


def test_list_paths_is_unaffected_by_a_symlinked_source(real, tmp_path: Path):
    """--list-paths reads only the trusted Dockerfile, so it answers as before, link or none."""
    trusted, checkout = real
    before = _run(trusted, '--context', str(checkout), '--list-paths', 'data', 'store')
    _link_out(checkout, 'server', tmp_path / 'elsewhere')
    after = _run(trusted, '--context', str(checkout), '--list-paths', 'data', 'store')
    assert before.returncode == after.returncode == 0, (before.stderr, after.stderr)
    assert after.stdout == before.stdout and after.stdout.split() == REAL_SOURCES[('data', 'store')]


def test_a_symlink_inside_a_copied_directory_is_recorded_not_refused(real, tmp_path: Path):
    """Below the source, links are unchanged: a link to a file and to a directory are digested by target."""
    trusted, checkout = real
    outside = tmp_path / 'outside'
    (outside / 'dir').mkdir(parents=True)
    (outside / 'file.py').write_text('one\n', encoding='utf-8')
    (outside / 'dir' / 'hidden.py').write_text('hidden\n', encoding='utf-8')
    (checkout / 'server' / 'common' / 'file_link.py').symlink_to(outside / 'file.py')
    (checkout / 'server' / 'common' / 'dir_link').symlink_to(outside / 'dir', target_is_directory=True)
    first = _digest(trusted, '--context', str(checkout), 'data', 'store')
    files = _run(trusted, '--context', str(checkout), '--list-files', 'data', 'store').stdout.splitlines()
    assert {'server/common/file_link.py', 'server/common/dir_link'} <= set(files), files
    assert not [path for path in files if path.startswith('server/common/dir_link/')], 'a directory link was walked'
    (outside / 'file.py').write_text('two\n', encoding='utf-8')
    (outside / 'dir' / 'hidden.py').write_text('changed\n', encoding='utf-8')
    assert _digest(trusted, '--context', str(checkout), 'data', 'store') == first, 'a link target was read'


def _own_copy(checkout: Path) -> Path:
    """The script and the real Dockerfile placed IN the checkout, so a no-flag run digests that checkout."""
    (checkout / 'tools').mkdir(exist_ok=True)
    shutil.copy2(SCRIPT, checkout / 'tools' / 'source_digest.sh')
    shutil.copyfile(REPO_ROOT / 'Dockerfile', checkout / 'Dockerfile')
    return checkout / 'tools' / 'source_digest.sh'


@pytest.mark.parametrize('component', ['server', 'server/data', 'gen'])
def test_without_the_flag_an_intermediate_link_is_walked_through_as_before(real, tmp_path: Path, component: str):
    """TESTS (ii): make dev-build's path is untouched; a link above a source still resolves.

    The digest is exactly the materialised tree's -- what the script printed before the guard existed.
    """
    _, checkout = real
    script = _own_copy(checkout)
    materialised = _digest(script, 'data', 'store')
    files = _run(script, '--list-files', 'data', 'store').stdout
    _link_out(checkout, component, tmp_path / 'elsewhere')
    assert _digest(script, 'data', 'store') == materialised
    assert _run(script, '--list-files', 'data', 'store').stdout == files


def test_without_the_flag_a_leaf_link_is_still_digested(real, tmp_path: Path):
    """TESTS (ii): a link AT a file source is recorded as a link, as before -- a digest, never a refusal."""
    _, checkout = real
    script = _own_copy(checkout)
    materialised = _digest(script, 'data', 'store')
    _link_out(checkout, 'uv.lock', tmp_path / 'elsewhere')
    linked = _run(script, 'data', 'store')
    assert linked.returncode == 0 and 'symlink' not in linked.stderr, (linked.returncode, linked.stderr)
    assert linked.stdout.strip() != materialised, 'a file source swapped for a link left the digest unchanged'
