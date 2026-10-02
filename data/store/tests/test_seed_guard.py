"""THE SEED GUARD: every database revision ships with its committed seed, or the PR gate fails (tj-irhy0a.4).

Design: decision tj-vhboky.55 (SEED FORMAT: <revision>.sql plus <revision>.json), storage ruled (a) on
tj-vhboky.56 -- committed files in tests/system/seeds/ -- with the user's ruling there: "a revision
without a seed fails". Seed naming as ratified on tj-irhy0a.4 (architect, 17:31 UTC 2026-09-29):

  * <rev>.sql / <rev>.json            the CANONICAL seed; rule 1 requires it for every revision;
  * <rev>.<variant>.sql / .json       an extra, hand-written bootstrap seed; it never stands in for
                                      the canonical one;
  * the revision is the text before the FIRST dot of the file name; rules 2-4 apply to every pair,
    variants included.

WHAT IS ASSERTED, against the revision graph read from data/store/migrations through alembic (never a
literal list) and the files in tests/system/seeds/:
  1. every revision in the graph, from the initial revision onward, has <rev>.sql and <rev>.json --
     less the exemption list;
  2. every seed file has its partner: a .sql without its .json, or the reverse, is half a seed;
  3. each manifest's "revision" field equals the revision its file name carries;
  4. no seed, canonical or variant, exists for a revision that is not in the graph (a stale or
     mistyped seed);
  5. a rule-1 failure names the missing revision and the command that produces its seed.
THE EXEMPTION LIST IS CLOSED AND LITERAL: only 2b88043cd13c, the initial revision, which predates any
data worth seeding. A second member needs an architect ruling; test_the_exemption_list_is_closed pins
it to exactly one member, the graph's base.

Each rule is a function over (graph, seed directory), run once against the repository and again
against tmp_path fixtures that break it, so every rule is shown red inside the gate itself, not only
by a one-off mutation.

PR-GATE TIER. This module sits in data/store/tests, not tests/system (pytest.ini's norecursedirs keeps
the system suite out of the gate). It reads files only: no database, no stack, no Docker.

COST TO CARRY FORWARD. From this guard onward, EVERY PR THAT ADDS A REVISION needs a run of the seed
producer at the new head before it can merge: either `make seed-dump SYSTEM_TEST_DISPOSABLE_DB=1` on
the host (against a stack from `make system-launch` then `make migrate`), or the agent-stack MCP's
seed_dump verb (ADR tj-4rr0la), which an agent can run itself. The resulting <rev>.sql and <rev>.json
are reviewed and committed to tests/system/seeds/. The PR is not mergeable until that seed is
committed: this module fails the gate naming the revision until it is.
"""

import json
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory


pytestmark = pytest.mark.data_store

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS_DIR = REPO_ROOT / 'data' / 'store' / 'migrations'
SEEDS_DIR = REPO_ROOT / 'tests' / 'system' / 'seeds'

# CLOSED. Adding a member needs an architect ruling (tj-irhy0a.4 item 2); the test below fails otherwise.
EXEMPT_REVISIONS = frozenset({'2b88043cd13c'})

SEED_SUFFIXES = ('.sql', '.json')

PRODUCE_COMMAND = (
    'produce it with `make seed-dump SYSTEM_TEST_DISPOSABLE_DB=1` (host) or the agent-stack MCP seed_dump verb '
    'at that revision, then commit its <revision>.sql and <revision>.json to tests/system/seeds/'
)


# ---------------------------------------------------------------------------------------------
# Inputs: the graph and the seed files.


def read_graph(migrations_dir: Path) -> dict[str, str | None]:
    """Every revision alembic loads from a migrations directory, mapped to its down_revision."""
    script = ScriptDirectory(str(migrations_dir))
    return {rev.revision: rev.down_revision for rev in script.walk_revisions()}


def seed_files(seeds_dir: Path) -> list[Path]:
    """Every .sql and .json file in the seed directory, sorted by name."""
    if not seeds_dir.is_dir():
        return []
    return sorted(path for path in seeds_dir.iterdir() if path.is_file() and path.suffix in SEED_SUFFIXES)


def seed_revision(path: Path) -> str:
    """The revision a seed file carries: the text before the first dot of its name."""
    return path.name.split('.', 1)[0]


# ---------------------------------------------------------------------------------------------
# The rules. Each returns a list of problems, one line each; empty means the rule holds.


def missing_canonical_seeds(revisions: set[str], exempt: frozenset[str], seeds_dir: Path) -> list[str]:
    """Rule 1 and rule 5: each non-exempt revision's <rev>.sql and <rev>.json, named with the command."""
    problems = []
    for revision in sorted(revisions - exempt):
        for suffix in SEED_SUFFIXES:
            if not (seeds_dir / f'{revision}{suffix}').is_file():
                problems.append(
                    f'revision {revision} has no {revision}{suffix} in tests/system/seeds/: {PRODUCE_COMMAND}'
                )
    return problems


def unpaired_seed_files(seeds_dir: Path) -> list[str]:
    """Rule 2: every .sql has its .json and every .json its .sql, variants included."""
    present = {path.name for path in seed_files(seeds_dir)}
    problems = []
    for name in sorted(present):
        stem, suffix = name.rsplit('.', 1)
        partner = f'{stem}.{"json" if suffix == "sql" else "sql"}'
        if partner not in present:
            problems.append(f'{name} has no partner {partner} in tests/system/seeds/: a seed is both files')
    return problems


def manifest_revision_mismatches(seeds_dir: Path) -> list[str]:
    """Rule 3: each manifest's revision field equals the revision its file name carries."""
    problems = []
    for path in seed_files(seeds_dir):
        if path.suffix != '.json':
            continue
        expected = seed_revision(path)
        try:
            manifest = json.loads(path.read_text(encoding='utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            problems.append(f'{path.name} is not a readable JSON manifest: {error}')
            continue
        actual = manifest.get('revision') if isinstance(manifest, dict) else None
        if actual != expected:
            problems.append(f'{path.name} names revision {actual!r} but its file name carries {expected!r}')
    return problems


def seeds_outside_the_graph(revisions: set[str], seeds_dir: Path) -> list[str]:
    """Rule 4: no seed file, canonical or variant, for a revision the graph does not hold."""
    return [
        f'{path.name} is a seed for revision {seed_revision(path)}, which is not in data/store/migrations '
        '(stale or mistyped): rename it to the revision it was produced at, or delete it'
        for path in seed_files(seeds_dir)
        if seed_revision(path) not in revisions
    ]


def _report(problems: list[str]) -> str:
    return '\n'.join(['the seed guard failed (tj-irhy0a.4):', *(f'  * {problem}' for problem in problems)])


# ---------------------------------------------------------------------------------------------
# The guard, against the repository.


@pytest.fixture(scope='module')
def graph() -> dict[str, str | None]:
    return read_graph(MIGRATIONS_DIR)


def test_the_guard_reads_a_real_graph_and_real_seeds(graph):
    """Vacuity guard: an empty graph or an empty seed directory would make every rule below hold trivially."""
    script = ScriptDirectory(str(MIGRATIONS_DIR))
    heads = script.get_heads()
    assert len(heads) == 1, f'expected one head in data/store/migrations, found {heads}'
    required = set(graph) - EXEMPT_REVISIONS
    assert heads[0] in required, f'the head {heads[0]} is not among the revisions the guard requires a seed for'
    assert seed_files(SEEDS_DIR), f'no seed files in {SEEDS_DIR}'


def test_every_revision_has_its_canonical_seed(graph):
    problems = missing_canonical_seeds(set(graph), EXEMPT_REVISIONS, SEEDS_DIR)
    assert not problems, _report(problems)


def test_every_seed_file_has_its_partner():
    problems = unpaired_seed_files(SEEDS_DIR)
    assert not problems, _report(problems)


def test_every_manifest_names_the_revision_its_file_name_carries():
    problems = manifest_revision_mismatches(SEEDS_DIR)
    assert not problems, _report(problems)


def test_no_seed_exists_for_a_revision_outside_the_graph(graph):
    problems = seeds_outside_the_graph(set(graph), SEEDS_DIR)
    assert not problems, _report(problems)


def test_the_exemption_list_is_closed(graph):
    """Exactly one member, the design's 2b88043cd13c, and it is the graph's base revision (tj-irhy0a.4 item 2).

    The literal is the design's value. The comparison with the graph's base makes sure the one
    exemption is the revision that predates any data, not a later one exempted by mistake.
    """
    bases = {revision for revision, down in graph.items() if down is None}
    assert len(EXEMPT_REVISIONS) == 1, (
        f'the exemption list must hold exactly one revision; it holds {sorted(EXEMPT_REVISIONS)} -- '
        'a further exemption needs an architect ruling on tj-irhy0a.4'
    )
    assert frozenset({'2b88043cd13c'}) == EXEMPT_REVISIONS
    assert bases == EXEMPT_REVISIONS, f'the exemption {sorted(EXEMPT_REVISIONS)} is not the graph base {sorted(bases)}'


# ---------------------------------------------------------------------------------------------
# The rules shown red, on tmp_path fixtures that break each one.

BASE, MIDDLE, HEAD = 'aaaaaaaaaaaa', 'bbbbbbbbbbbb', 'cccccccccccc'
FIXTURE_GRAPH = {BASE, MIDDLE, HEAD}
FIXTURE_EXEMPT = frozenset({BASE})


def _seed(seeds_dir: Path, name: str, revision: str | None = None) -> None:
    """Write <name>.sql and <name>.json, the manifest naming `revision` (default: the name's own)."""
    seeds_dir.mkdir(exist_ok=True)
    (seeds_dir / f'{name}.sql').write_text('INSERT INTO public.t ("id") VALUES (1);\n', encoding='utf-8')
    manifest = {'revision': revision or name.split('.', 1)[0], 'producer': 'data.store.seeds'}
    (seeds_dir / f'{name}.json').write_text(json.dumps(manifest), encoding='utf-8')


@pytest.fixture
def complete(tmp_path) -> Path:
    """A seed directory that satisfies every rule for FIXTURE_GRAPH, with one variant beside a canonical seed."""
    seeds = tmp_path / 'seeds'
    for name in (MIDDLE, HEAD, f'{MIDDLE}.entries-only'):
        _seed(seeds, name)
    return seeds


def test_a_complete_fixture_passes_every_rule(complete):
    """The baseline the red cases below each break in one way."""
    assert missing_canonical_seeds(FIXTURE_GRAPH, FIXTURE_EXEMPT, complete) == []
    assert unpaired_seed_files(complete) == []
    assert manifest_revision_mismatches(complete) == []
    assert seeds_outside_the_graph(FIXTURE_GRAPH, complete) == []


@pytest.mark.parametrize('suffix', SEED_SUFFIXES)
def test_a_missing_canonical_file_fails_naming_the_revision_and_the_command(complete, suffix):
    (complete / f'{HEAD}{suffix}').unlink()
    (problem,) = missing_canonical_seeds(FIXTURE_GRAPH, FIXTURE_EXEMPT, complete)
    assert f'revision {HEAD} has no {HEAD}{suffix}' in problem
    assert 'make seed-dump' in problem
    assert 'seed_dump verb' in problem
    assert 'commit' in problem and 'tests/system/seeds/' in problem


def test_a_variant_does_not_stand_in_for_the_canonical_seed(complete):
    for suffix in SEED_SUFFIXES:
        (complete / f'{MIDDLE}{suffix}').unlink()
    assert (complete / f'{MIDDLE}.entries-only.sql').is_file()
    problems = missing_canonical_seeds(FIXTURE_GRAPH, FIXTURE_EXEMPT, complete)
    assert [problem.split(':', 1)[0] for problem in problems] == [
        f'revision {MIDDLE} has no {MIDDLE}.sql in tests/system/seeds/',
        f'revision {MIDDLE} has no {MIDDLE}.json in tests/system/seeds/',
    ]


def test_only_the_exempt_revision_goes_without_a_seed(complete):
    assert not (complete / f'{BASE}.sql').exists()
    problems = missing_canonical_seeds(FIXTURE_GRAPH, frozenset(), complete)
    assert len(problems) == 2 and all(f'revision {BASE} ' in problem for problem in problems)


def test_a_missing_seed_directory_fails_every_required_revision(tmp_path):
    problems = missing_canonical_seeds(FIXTURE_GRAPH, FIXTURE_EXEMPT, tmp_path / 'absent')
    assert len(problems) == 2 * len(FIXTURE_GRAPH - FIXTURE_EXEMPT)


@pytest.mark.parametrize(('name', 'suffix'), [(HEAD, '.json'), (HEAD, '.sql'), (f'{MIDDLE}.entries-only', '.json')])
def test_half_a_seed_fails(complete, name, suffix):
    (complete / f'{name}{suffix}').unlink()
    (problem,) = unpaired_seed_files(complete)
    assert problem.startswith(f'{name}.{"sql" if suffix == ".json" else "json"} has no partner {name}{suffix}')


@pytest.mark.parametrize('name', [HEAD, f'{MIDDLE}.entries-only'])
def test_a_manifest_naming_another_revision_fails(complete, name):
    _seed(complete, name, revision=MIDDLE if name == HEAD else HEAD)
    (problem,) = manifest_revision_mismatches(complete)
    assert problem.startswith(f'{name}.json names revision')


@pytest.mark.parametrize('content', ['{not json', '["a list"]', '{"producer": "data.store.seeds"}'])
def test_an_unreadable_or_revisionless_manifest_fails(complete, content):
    (complete / f'{HEAD}.json').write_text(content, encoding='utf-8')
    (problem,) = manifest_revision_mismatches(complete)
    assert problem.startswith(f'{HEAD}.json ')


@pytest.mark.parametrize('name', ['dddddddddddd', 'dddddddddddd.collision-free'])
def test_a_seed_for_a_revision_outside_the_graph_fails(complete, name):
    _seed(complete, name)
    problems = seeds_outside_the_graph(FIXTURE_GRAPH, complete)
    assert [problem.split(' ', 1)[0] for problem in problems] == [f'{name}.json', f'{name}.sql']
    assert all('dddddddddddd, which is not in data/store/migrations' in problem for problem in problems)


def test_a_variant_of_a_graph_revision_is_not_stale(complete):
    assert seeds_outside_the_graph({MIDDLE, HEAD}, complete) == []
    assert seeds_outside_the_graph({HEAD}, complete) != []
