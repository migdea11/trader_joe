"""The layout itself: the facts the move established that NOTHING ELSE WOULD NOTICE BREAKING.

Epic tj-iontkq, task tj-iontkq.11 (V1). Every assertion here exists because the thing it checks
FAILS SILENTLY -- a green lint run and a green test run prove none of them. That is the whole
justification for the file, and it is the test to apply to anything added to it: if `make lint` or
the rest of `make test` would already go red, it does not belong here.

WHAT IS *NOT* HERE, because it is already pinned elsewhere and a second copy would be a second
thing to keep in step:
  A-2, the two sentinels having diverged      -> test_roots.py::test_the_two_roots_are_distinct_in_this_checkout
                                                 and ::test_the_server_root_is_the_server_directory_under_the_repository_root
  A-5, every Dockerfile COPY source existing  -> test_ci_invariants.py::test_dockerignore_excludes_no_dockerfile_copy_source,
                                                 which already skips --from= stages and expands
                                                 ${SERVICE_PATH}/${SERVICE_NAME} from compose build args
  A-8, the wheel's packages list              -> there is no [build-system] and no [tool.hatch...]
                                                 table in this repository yet. tj-iontkq.6 adds
                                                 server/pyproject.toml and owns the assertion; one
                                                 written here now would assert about nothing.

WHAT NO TEST IN THIS REPOSITORY CAN COVER, stated so a green run is not overread: the image
building or starting, the compose dev override hot-reloading, alembic finding its migrations, and
the CI System Testing job. Docker does not run in the agent container. Those belong to
tj-iontkq.12 and no agent may claim them from here.
"""

import re
import tomllib
from pathlib import Path

import pytest

from common.tests import compose_model
from common.tests.roots import REPO_ROOT, SERVER_ROOT


pytestmark = pytest.mark.build_infra

PYPROJECT = tomllib.loads((REPO_ROOT / 'pyproject.toml').read_text(encoding='utf-8'))


# =================================================================================================
# A-1. THE PACKAGE MARKERS THAT MUST NOT EXIST.
# =================================================================================================


def test_the_server_directory_is_not_a_package():
    """One empty server/__init__.py breaks 252 import lines at once, and nothing else looks.

    pytest's prepend import mode walks UP from a collected test module for as long as it keeps
    finding __init__.py, and the first directory WITHOUT one becomes the sys.path entry the module
    is imported from. server/common/tests/__init__.py exists, server/common/__init__.py exists, and
    server/ carrying none is what stops that walk at server/ -- which is exactly why `import
    common` resolves. Add the file and the walk climbs one further, to the repository root:
    `common` becomes `server.common`, and every first-party import in the tree fails at once.

    It is the cheapest possible way to destroy this layout: one empty file, no diff to read, and
    `make lint` stays green because an empty __init__.py is valid Python. pytest.ini:30-33 states
    the invariant in prose; this is the only place it is asserted.

    THIS ONE CANNOT BE SEEN RED, AND THAT IS WORTH KNOWING RATHER THAN HIDING. Measured: with
    server/__init__.py present, `make test PATHS=server/common/tests` collects ZERO items and ends
    at `ModuleNotFoundError: No module named 'common'` -- the hazard destroys the import of this
    very module before the assertion can run. So this is not a tripwire; the suite already trips,
    deafeningly. What it is, is the EXPLANATION: the message below is the only place in the
    repository that connects that cryptic ModuleNotFoundError to the one empty file that caused
    it. The two assertions beside it -- the client marker and the pytest configuration -- are the
    genuinely silent ones, and both have been seen red.
    """
    marker = REPO_ROOT / 'server' / '__init__.py'
    assert not marker.exists(), (
        f"{marker} exists. server/ MUST NOT be a package: with it, pytest's basedir walk-up climbs "
        f'past server/ to the repository root, `import common` becomes `import server.common`, and '
        f'every first-party import in the tree breaks at once. Delete it. If a package named server '
        f'is genuinely wanted, that is a layout change and belongs in a decision record, not here.'
    )


def test_the_client_directory_is_not_a_package_at_its_root():
    """The same hazard at the other new home, pinned before there is anything there to break.

    client/ holds a README today (tj-iontkq.1; tj-iontkq.7 makes it a real buildable skeleton). The
    package it will ship lives UNDER it -- client/<dist>/ -- not at client/ itself, for the same
    reason server/ carries no marker: the directory is a HOME for a tree, not a tree.
    """
    marker = REPO_ROOT / 'client' / '__init__.py'
    assert not marker.exists(), (
        f'{marker} exists. client/ is a home for a distribution, not a package itself; the shipped '
        f'package belongs one level down. See the server/ case above for the import breakage this '
        f'shape causes.'
    )


@pytest.mark.parametrize('name', ['pytest.ini', 'pyproject.toml', 'setup.cfg', 'tox.ini'])
def test_the_server_directory_carries_no_pytest_configuration(name: str):
    """THE SECOND KILL SWITCH, and it is live rather than hypothetical: tj-iontkq.6 adds server/pyproject.toml.

    pytest picks its rootdir and its config from the NEAREST ancestor of the collected paths that
    carries one of these, so a config file appearing at server/ would silently move rootdir from
    the repository root to server/. `pythonpath = . gen/proto/python` is rootdir-relative, so `.`
    would stop meaning the repository root; tests/ and tools/ would stop being importable; and
    common/tests/roots.py's REPO_MARKER search would find pytest.ini at server/ and collapse the
    two sentinels back together -- the exact regression test_roots.py exists to catch, arriving
    from a direction test_roots.py cannot see.

    pyproject.toml is in this list for the SAME reason and only through one table: pytest reads
    [tool.pytest.ini_options] and ignores a pyproject.toml without it. tj-iontkq.6 will create
    server/pyproject.toml for the trader-joe-common wheel, so the assertion is on the TABLE, not
    on the file -- a flat refusal of the file would be wrong the day .6 lands, and would be
    deleted rather than corrected.
    """
    path = SERVER_ROOT / name
    if name != 'pyproject.toml':
        assert not path.exists(), (
            f"{path} exists. A pytest configuration at the server root moves pytest's rootdir off "
            f'the repository root, which breaks `pythonpath = .` and collapses REPO_ROOT onto '
            f'SERVER_ROOT. The repository has exactly one pytest configuration and it lives at the top.'
        )
        return
    if not path.is_file():
        return  # tj-iontkq.6 has not landed yet; the table check below is what matters when it does.
    assert 'pytest' not in tomllib.loads(path.read_text(encoding='utf-8')).get('tool', {}), (
        f"{path} declares [tool.pytest.ini_options]. That makes server/ pytest's rootdir: "
        f'`pythonpath = .` stops meaning the repository root, tests/ and tools/ stop being '
        f'importable, and REPO_ROOT collapses onto SERVER_ROOT. The packaging tables are fine; '
        f'the pytest table is not.'
    )


# =================================================================================================
# A-3. ruff's known-first-party, pinned by EQUALITY.
# =================================================================================================

# The import names ruff must classify as first-party, and nothing else. IMPORT NAMES, NOT PATHS:
# this list did not take the server/ prefix in the move and must not, because `import common` is
# still spelled `common` -- which is the whole point of the chosen layout. The two globbed tables
# above it in pyproject.toml ARE paths and did take the prefix; A-4 below pins those.
FIRST_PARTY = ('common', 'data', 'routers', 'schemas', 'trader_joe')


def test_ruff_known_first_party_is_exactly_the_five_import_roots():
    """EQUALITY, not containment, and the linter cannot be the check.

    Miss a name and every import of it reclassifies as third-party -- and LINT STAYS GREEN, because
    consistently mis-sorted imports are still consistently sorted. Running ruff proves nothing
    here; only an equality assertion on the list does.

    AN EXTRA NAME IS AS WRONG AS A MISSING ONE, and it is the failure this epic actually invites:
    adding `server` here would be the natural-looking reaction to the move and would reclassify
    nothing correctly while silently absorbing any future third-party package of that name. So the
    assertion is on the exact set AND on the length, which is what rejects a duplicate entry too.
    """
    declared = PYPROJECT['tool']['ruff']['lint']['isort']['known-first-party']
    assert sorted(declared) == sorted(FIRST_PARTY), (
        f'ruff known-first-party is {sorted(declared)}, expected exactly {sorted(FIRST_PARTY)}. '
        f'These are IMPORT NAMES and did not move: adding `server` or a path spelling here is wrong. '
        f'Extra: {sorted(set(declared) - set(FIRST_PARTY))}. Missing: {sorted(set(FIRST_PARTY) - set(declared))}.'
    )
    assert len(declared) == len(FIRST_PARTY), f'known-first-party names something twice: {declared}'


# =================================================================================================
# A-4. EVERY ruff PATH GLOB RESOLVES TO SOMETHING THAT EXISTS.
# =================================================================================================

# The two the bead named, as a NON-VACUITY FLOOR rather than as the list. Deriving the globs from
# the config and then requiring these two to be among them is the difference between a test that
# notices a new glob and one that quietly stops covering it; this epic has already produced three
# hand-maintained lists that were short, so the list here is computed and only the floor is written.
NAMED_PATH_GLOBS = ('server/data/store/migrations/versions/*', 'server/routers/**')


def _ruff_path_globs() -> list[str]:
    """Every ruff setting that is a PATH pattern rather than an import name or a rule code.

    `exclude` entries and `per-file-ignores` keys are both resolved against pyproject.toml's own
    directory, so both took the server/ prefix in the move. A bare name with no separator (".beads",
    "__pypackages__") is a filename pattern matched at any depth, not a rooted path, so it has no
    prefix to go stale and is left out.
    """
    ruff = PYPROJECT['tool']['ruff']
    patterns = [*ruff.get('exclude', []), *ruff['lint'].get('per-file-ignores', {})]
    return [pattern for pattern in patterns if '/' in pattern]


def test_the_two_named_path_globs_are_among_the_ones_found():
    """The floor. If this fails, the derivation below changed shape and is no longer covering them."""
    found = _ruff_path_globs()
    assert set(NAMED_PATH_GLOBS) <= set(found), f'{sorted(set(NAMED_PATH_GLOBS) - set(found))} not found in {found}'


@pytest.mark.parametrize('pattern', _ruff_path_globs())
def test_every_ruff_path_glob_has_a_real_directory_prefix(pattern: str):
    """A glob that stops matching stops doing its job, and ruff never says a pattern matched nothing.

    THE BEAD'S PREMISE -- "a glob that stops matching does not fail lint" -- IS NOT TRUE OF THESE
    TWO TODAY, and saying so is the point of writing it down. Measured on this branch, with
    `make lint PATHS=.` otherwise at exit 0:
      "server/routers/**" -> "routers/**" (stale prefix):        lint exit 2
      "server/data/.../versions/*" -> "data/.../versions/*":     lint exit 2, "Found 25 errors."
    Both go LOUD, because the files they stop covering happen to have findings right now -- B008 on
    every FastAPI Depends() handler, and 25 findings in the generated alembic revisions.

    SO WHY KEEP THE TEST. Two reasons, and neither is the one the bead gave.
      1. The loudness is CONTINGENT, not structural. It is a property of what those files contain
         today, not of the glob. A per-file-ignore over a tree that is currently clean, or an
         exclude over a directory holding no .py (`web/**` is exactly that), goes stale in total
         silence. Nothing distinguishes those cases from these except today's findings.
      2. When it IS loud, it is loud in the WRONG PLACE. The report names 25 findings in generated
         revisions; it does not name the glob. The obvious-looking repair is to silence the
         findings -- a noqa sweep over generated files, or a new ignore -- and that repair leaves
         the stale glob exactly where it was. This test names the glob.

    What is asserted is the non-glob PREFIX: every leading component with no wildcard in it must be
    a path that exists. That is the part a rename invalidates. Whether the glob then matches any
    .py today is deliberately not asserted -- `**/tests/**` legitimately matches a tree that may be
    empty, and ruff itself never errors on an unmatched pattern.
    """
    components = pattern.split('/')
    prefix = [c for c in components[: -1 if components[-1] else None] if not re.search(r'[*?\[]', c)]
    if not prefix:
        return  # e.g. "**/tests/**": nothing is rooted, so there is no prefix to go stale.
    resolved = REPO_ROOT.joinpath(*prefix)
    assert resolved.exists(), (
        f'ruff pattern {pattern!r} has the non-glob prefix {"/".join(prefix)!r}, which does not '
        f'exist at {resolved}. The pattern matches nothing and ruff will not say so: an exclude '
        f'silently stops excluding, a per-file-ignore silently stops ignoring. Most likely a tree '
        f'moved and this pattern did not follow it.'
    )


# =================================================================================================
# A-6. EVERY HOST PATH THE COMPOSE FILES NAME EXISTS.
# =================================================================================================

COMPOSE_FILES = ('docker-compose.yaml', 'docker-compose.override.yaml')


def _resolved(text: str) -> str:
    """One compose string with its path defaults applied, and nothing else.

    INTERPOLATED PER STRING, NOT PER DOCUMENT. compose_model.interpolate_tree over a whole file
    raises on the first `${VAR:?...}` guard it meets -- docker-compose.yaml has several, starting
    with POSTGRES_PASS -- and those guards are deliberate and nowhere near a path. Only the volume
    sources and env_file entries matter here, and those use `:-` defaults like
    `${DATA_DIR:-./volume_data}`, which resolve under an empty environment to exactly the path a
    developer with no overrides gets. That is the checkout this test is about.
    """
    return compose_model.interpolate(text, {})


# The one bind source that is RUNTIME STATE rather than a path out of the checkout: the data
# directory, `${DATA_DIR:-./volume_data}/...`. It is gitignored (.gitignore:32), docker creates it
# on first start, and a fresh clone and CI both have none -- so asserting it exists would fail
# everywhere that matters and the assertion would be deleted rather than corrected. Matched on the
# RAW source before interpolation, so it names the variable rather than today's default directory.
RUNTIME_BIND_PREFIX = '${DATA_DIR'


def _host_binds(path: Path) -> list[tuple[str, str]]:
    """(service, host path) for every bind mount out of the CHECKOUT the compose file declares.

    Short and long form both, via compose_model.volume. Runtime data mounts are left out: see
    RUNTIME_BIND_PREFIX.
    """
    document = compose_model.load(path)
    found = []
    for service, body in (document.get('services') or {}).items():
        for entry in body.get('volumes') or []:
            volume = compose_model.volume(entry)
            if volume['type'] == 'bind' and not volume['source'].startswith(RUNTIME_BIND_PREFIX):
                found.append((service, _resolved(volume['source'])))
    return found


def _env_file_entries(path: Path) -> list[tuple[str, str]]:
    """(service, entry) for every env_file entry the compose file declares."""
    document = compose_model.load(path)
    found = []
    for service, body in (document.get('services') or {}).items():
        entries = body.get('env_file') or []
        for entry in [entries] if isinstance(entries, str) else entries:
            found.append((service, _resolved(entry if isinstance(entry, str) else entry['path'])))
    return found


@pytest.mark.parametrize('name', COMPOSE_FILES)
def test_every_compose_bind_mount_names_a_host_path_that_exists(name: str):
    """A bind mount whose host side is gone does not fail: docker CREATES AN EMPTY DIRECTORY there.

    That is the whole hazard and it is why this is a test rather than something the stack would
    tell you. A container started over an empty mount imports nothing, or imports a stale copy
    baked into the image, and the symptom surfaces as a missing module three layers away from the
    compose file that caused it. `compose up` says nothing at all.

    Named volumes are excluded by compose_model.volume, which classifies a source as a bind only
    when it starts with . / ~ or $ -- a named volume has no host path to check.
    """
    path = REPO_ROOT / name
    binds = _host_binds(path)
    assert binds, f'{name} declares no bind mounts; this test would be vacuous'
    missing = [
        f'{name}: service {service} mounts {source}, which does not exist'
        for service, source in binds
        if not (REPO_ROOT / source).exists()
    ]
    assert not missing, 'compose bind mounts with no host path:\n' + '\n'.join(missing)


@pytest.mark.parametrize('name', COMPOSE_FILES)
def test_every_compose_env_file_resolves_to_a_real_directory(name: str):
    """THE DIRECTORY, NOT THE FILE, and the distinction is load-bearing.

    Every .env this repository loads is GITIGNORED -- it holds the deployment's own values and is
    made from the .env.default committed beside it. So a clean checkout has the directory and the
    template and not the file, and asserting the file would fail on every fresh clone and in CI,
    which is how an assertion gets deleted. What a rename actually breaks is the DIRECTORY, so that
    is what is asserted, plus the committed template beside it where one exists.
    """
    path = REPO_ROOT / name
    entries = _env_file_entries(path)
    missing = []
    for service, entry in entries:
        resolved = REPO_ROOT / entry
        if not resolved.parent.is_dir():
            missing.append(f'{name}: service {service} loads {entry}, whose directory does not exist')
        elif not resolved.exists() and not resolved.with_name(f'{resolved.name}.default').is_file():
            missing.append(f'{name}: service {service} loads {entry}, and neither it nor {entry}.default exists')
    assert not missing, 'compose env_file entries with no home:\n' + '\n'.join(missing)


def test_the_compose_files_declare_env_file_entries_to_check():
    """The non-vacuity floor for the pair, not for each file: the override declares none, by design.

    Asserted across the two rather than inside the parametrised test above, so the day the override
    grows an env_file nothing has to be rewritten, and the day the base file LOSES all of them this
    still fails instead of passing on an empty list.
    """
    entries = [entry for name in COMPOSE_FILES for entry in _env_file_entries(REPO_ROOT / name)]
    assert entries, f'{COMPOSE_FILES} declare no env_file entry at all; the check above is vacuous'


# =================================================================================================
# A-7. NO STALE REPOSITORY-ROOT-RELATIVE TREE NAME SURVIVES IN A FILE A TOOL READS.
# =================================================================================================
#
# WHAT THIS SCANS, AND THE DEVIATION FROM THE BEAD, stated here rather than in a report because a
# report is not where the next reader looks. tj-iontkq.11 listed CLAUDE.md, README.md and docs/**
# alongside the build files. Measured on this branch, the bead's pattern hits 135 lines across 18
# files; 105 of those are PROSE -- a comment or a markdown sentence naming a module by its old path
# ("common/errors", "data/store/migrations/env.py"). An allow-list of 105 prose entries is not a
# control, it is a chore, and the bead's own warning says this test gets deleted the first time it
# fires wrongly. So the scan is narrowed to the thing that FAILS SILENTLY: a path some tool
# RESOLVES. Prose that names a moved module is documentation drift -- real, worth fixing, owned by
# tj-iontkq.9 (CLAUDE.md, .claude/workflow.yml's prose fields) and tj-iontkq.10 (README.md, docs/**)
# -- and it is not what a stale path costs. THE LIMIT, stated so a green run is not overread: a
# wrong path inside a comment in a scanned file is NOT caught here.

SCANNED = ('Makefile', 'pyproject.toml', 'pytest.ini', 'Dockerfile', '.dockerignore', 'entrypoint.sh')
SCANNED_GLOBS = ('docker-compose*.yaml', '.github/workflows/*.yml', '.github/workflows/*.yaml')

# Every way a step can SPELL the checkout root before a repository-relative path. The same list
# test_ci_invariants.py's _CHECKOUT_ROOT carries, and it is here for the reason that one exists:
# `${PWD}/data/store/.env` is just as root-relative as `data/store/.env`, and a pattern that
# anchors only on "no path character before it" skips every one of these. MEASURED -- an earlier
# draft of this test missed exactly those lines in the CI workflow while catching the bare ones
# beside them, which is the half-covering shape this epic keeps producing.
CHECKOUT_ROOT = (
    r'(?:\./|\$\{PWD\}/|\$PWD/|\$\{GITHUB_WORKSPACE\}/|\$GITHUB_WORKSPACE/|\$\{\{\s*github\.workspace\s*\}\}/)?'
)

# The four trees that moved, as the FIRST component of a repository-root-relative path. The
# lookbehind is what makes this specific rather than noisy: it rejects a match preceded by any path
# or word character, so `server/common/` (correct), `/code/common` (container-side, legitimate and
# staying), `gen/proto` and `metadata/` are all passed over -- while the alternation above still
# admits the explicit root spellings, each of which ENDS in `/` and so would otherwise be rejected
# by that same lookbehind. Hence the lookbehind sits in front of the whole thing, not in front of
# the tree name.
MOVED_TREES = ('common', 'data', 'routers', 'schemas')
ROOT_RELATIVE = re.compile(rf'(?<![\w./\\-]){CHECKOUT_ROOT}(?:{"|".join(MOVED_TREES)})/')

# Each entry is a scanned path whose remaining hits are ALLOWED, with the reason. An entry here is
# a claim that every match in that file is legitimate -- so the test also fails when a listed file
# becomes clean, which is what stops an entry outliving its reason.
# WHAT IT FOUND WHEN IT WAS WRITTEN, recorded because the number is the point. Every scanned file
# is clean except .github/workflows/trader_joe_testing.yml, where it names NINETEEN lines: 201,
# 206, 312, 313, 377, 378, 409, 410, 419, 420, 509-512, 521-523, 1269, 1270. tj-iontkq.11's own
# text said thirteen and tj-iontkq.8's agent re-derived nineteen by hand; this test reaches the
# same nineteen from a third direction, which is the whole argument for deriving a list instead of
# writing one down. tj-iontkq.8 owns those lines and this goes green when it lands -- and if its
# fix is partial, this names what is left rather than passing.
ALLOWED: dict[str, str] = {
    # The one match is inside a banned-api MESSAGE: "...is private to common/rpc; use the
    # hand-written API there". Human prose in a string ruff prints, not a path anything opens.
    'pyproject.toml': 'a banned-api message naming common/rpc as a module, not a path'
}


def _scanned_files() -> list[Path]:
    found = [REPO_ROOT / name for name in SCANNED if (REPO_ROOT / name).is_file()]
    for glob in SCANNED_GLOBS:
        found.extend(sorted(REPO_ROOT.glob(glob)))
    return sorted(set(found))


def _stale_lines(path: Path) -> list[str]:
    """Every line of PATH naming a moved tree at the repository root, outside a comment.

    The comment rule is one heuristic for all of these formats, because every one of them -- make,
    toml, ini, Dockerfile, shell, YAML -- starts a comment with `#`: a match with a `#` anywhere
    before it on the line is prose. It can under-report (a `#` inside an earlier quoted string
    hides the rest of that line) and the docstring on the test says so.
    """
    stale = []
    for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        match = ROOT_RELATIVE.search(line)
        if match and '#' not in line[: match.start()]:
            stale.append(f'{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}')
    return stale


def test_the_scan_reaches_the_files_it_claims_to():
    """A glob that matches nothing would make every assertion below vacuously true."""
    found = {str(path.relative_to(REPO_ROOT)) for path in _scanned_files()}
    required = {'Makefile', 'Dockerfile', 'pyproject.toml', 'pytest.ini', 'docker-compose.yaml'}
    assert required <= found, f'not scanned: {sorted(required - found)}'
    assert any(name.startswith('.github/workflows/') for name in found), f'no workflow scanned; found {sorted(found)}'


@pytest.mark.parametrize('path', _scanned_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_build_or_ci_file_names_a_moved_tree_at_the_repository_root(path: Path):
    """common/, data/, routers/ and schemas/ live under server/ now, and a stale path FAILS OPEN.

    This is the shape the epic is about. `cp data/store/.env.default data/store/.env` in a workflow
    does not fail loudly when data/ is no longer there -- cp fails, or worse it succeeds against a
    directory something else created, and the job carries on with an env file nobody wrote. A
    compose bind over a missing host path gets an empty directory. A Makefile variable pointing at
    a gone tree hands a scanner nothing and the scanner reports zero findings, which reads as pass.
    None of it reaches the PR gate, because the jobs that would notice are the ones the gate does
    not run.

    THE LIMIT: this reads lines, not semantics. A path inside a comment is skipped (see
    _stale_lines), and a path that exists but is WRONG is not something a pattern can see.
    """
    name = str(path.relative_to(REPO_ROOT))
    stale = _stale_lines(path)
    if name in ALLOWED:
        assert stale, (
            f'{name} is in ALLOWED ("{ALLOWED[name]}") but now has no match at all. The entry has '
            f'outlived its reason: delete it from ALLOWED rather than leaving it to excuse a future one.'
        )
        return
    assert not stale, (
        f'{name} names a moved tree as a repository-root-relative path. The four service trees live '
        f'under server/ (tj-iontkq.4); add the prefix, or add {name} to ALLOWED with the reason it '
        f'is legitimate.\n' + '\n'.join(stale)
    )
