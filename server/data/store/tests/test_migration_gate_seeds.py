"""The migration gate's seed leg is never vacuous: a seed below the head is upgraded to the head (bead tj-7zjov1.11).

Design checked against: PR5-PLAN N1 row P05 on epic tj-7zjov1 and its item m2 (CI applies every revision from
empty AND upgrades the previous release's seed with data), and the architect's 11:25 UTC 2026-10-24 note on
tj-7zjov1.11: pin that at least one seed sits strictly below the head, so the seed leg can never pass by loading
seeds at the head alone.

THE LOGIC (the rule itself is in server/common/tests/migration_gate.py's docstring): the CI Migration Gate's seed step runs
tests/system/test_migration_with_data.py, whose to-head test is parametrised over seeds_upgraded_to_head(). That
list is the canonical seeds strictly below the head whose path to the head crosses no marked revision. If it were
empty, the parametrisation would be empty and the step would pass having upgraded no data. So this test fails
when (a) no canonical seed sits below the head, whatever else is in tests/system/seeds, or (b) the newest one
below the head (the previous release's, when a release adds one revision) is not selected, which happens when the
head or a revision between is marked: then the release needs an expectation, not a lossless to-head upgrade, and
the failure says so. A seed at the head alone is case (a).

PR-GATE TIER: files only, no database. The upgrade itself runs in the system suite.
"""

import pytest

from common.tests.migration_gate import Step, canonical_seed_revisions, read_line, seeds_upgraded_to_head
from common.tests.roots import REPO_ROOT, SERVER_ROOT


pytestmark = pytest.mark.data_store

MIGRATIONS_DIR = SERVER_ROOT / 'data' / 'store' / 'migrations'
SEEDS_DIR = REPO_ROOT / 'tests' / 'system' / 'seeds'


def test_a_seed_below_the_head_is_upgraded_to_the_head():
    line = read_line(MIGRATIONS_DIR)
    assert len(line) >= 2, f'the revision line {[step.revision for step in line]} has no revision below the head'
    head = line[-1].revision
    seeded = canonical_seed_revisions(SEEDS_DIR)
    below = [step.revision for step in line[:-1] if step.revision in seeded]
    assert below, (
        f'no canonical seed in {SEEDS_DIR} sits below the head {head}: the Migration Gate seed leg would upgrade no '
        'data (seeds at the head alone migrate nothing)'
    )
    selected = seeds_upgraded_to_head(line, seeded)
    assert below[-1] in selected, (
        f'the newest seed below the head, {below[-1]}, cannot be upgraded to {head} losslessly: a revision on its '
        'path carries the additive-exception marker, so the release needs its expectation written in '
        'tests/system/test_migration_with_data.py EXPECTATIONS and a ruling on what the to-head check asserts'
    )
    assert head not in selected


# --- the rule, on synthetic lines ------------------------------------------------------------------------

A, B, C, D = 'aaaaaaaaaaaa', 'bbbbbbbbbbbb', 'cccccccccccc', 'dddddddddddd'


def _line(*marked: str) -> list[Step]:
    return [Step(revision, revision in marked) for revision in (A, B, C, D)]


def test_every_seed_below_an_unmarked_path_is_selected_oldest_first():
    assert seeds_upgraded_to_head(_line(), {A, B, C, D}) == [A, B, C]


def test_a_seed_at_the_head_alone_selects_nothing():
    assert seeds_upgraded_to_head(_line(), {D}) == []


def test_a_marked_revision_excludes_every_seed_whose_path_crosses_it():
    assert seeds_upgraded_to_head(_line(B), {A, B, C}) == [B, C]
    assert seeds_upgraded_to_head(_line(D), {A, B, C}) == []


def test_only_canonical_seeds_count(tmp_path):
    for name in (A, f'{B}.collision-free', C):
        (tmp_path / f'{name}.sql').write_text('INSERT INTO t VALUES (1);\n', encoding='utf-8')
        (tmp_path / f'{name}.json').write_text('{}', encoding='utf-8')
    (tmp_path / f'{D}.sql').write_text('', encoding='utf-8')  # half a seed
    assert canonical_seed_revisions(tmp_path) == {A, C}
    assert canonical_seed_revisions(tmp_path / 'absent') == set()
