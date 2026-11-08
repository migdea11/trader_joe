"""Which committed seeds the migration gate upgrades all the way to the head (bead tj-7zjov1.11, m2).

No test functions here. Read by the PR-gate pin (server/data/store/tests/test_migration_gate_seeds.py, files
only) and by the system suite's to-head upgrade (tests/system/test_migration_with_data.py, on Postgres), so the
two select the same seeds from one rule. It sits in common/tests, not data/store/tests, because the test client
container mounts server/common and not server/data/store/tests (docker-compose.test-client.yaml), the same
reason image_path.py and roots.py live here.

THE RULE. A production database sits at the previous release's head with data in it, and the next deploy runs
`alembic upgrade head` once, across every revision the release adds. The seed that stands for it is a CANONICAL
seed (<revision>.sql and <revision>.json, never a variant) at a revision STRICTLY BELOW the head: a seed at the
head upgrades nothing, so a gate that only loaded seeds at the head would pass having migrated no data. The
seed's path to the head must also cross no revision carrying the additive-exception marker: such a revision may
refuse or wipe data by design, so "lossless to the head" is not its expectation, and the one-step generic rule
holds its EXPECTATIONS instead. A seed that is selected is upgraded to the head in ONE invocation and must
arrive with every manifest column intact.

The head's parent is always selected unless the head itself is marked. Today the seed guard requires a
canonical seed at every revision but the base, so that seed exists; the PR-gate pin asserts it independently,
because "the seed leg upgraded something" is the claim the CI Migration Gate makes and it should not rest on a
different test's rule.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from alembic.script import ScriptDirectory


ADDITIVE_EXCEPTION_MARKER = re.compile(r'^# additive-exception: \S+', re.MULTILINE)


@dataclass(frozen=True)
class Step:
    revision: str
    marked: bool


def read_line(migrations_dir: Path) -> list[Step]:
    """The revision line from the base to the head, each with whether it carries the marker."""
    script = ScriptDirectory(str(migrations_dir))
    heads = script.get_heads()
    assert len(heads) == 1, f'expected one head in {migrations_dir}, found {heads}'
    steps = []
    for rev in script.walk_revisions():  # head first
        assert rev.down_revision is None or isinstance(rev.down_revision, str), f'{rev.revision} is a merge point'
        text = Path(rev.path).read_text(encoding='utf-8')
        steps.append(Step(rev.revision, bool(ADDITIVE_EXCEPTION_MARKER.search(text))))
    return list(reversed(steps))


def canonical_seed_revisions(seeds_dir: Path) -> set[str]:
    """Revisions with both canonical seed files; a variant (<revision>.<variant>) never counts."""
    if not seeds_dir.is_dir():
        return set()
    stems = {path.stem for path in seeds_dir.glob('*.sql')} & {path.stem for path in seeds_dir.glob('*.json')}
    return {stem for stem in stems if '.' not in stem}


def seeds_upgraded_to_head(line: list[Step], seeded: set[str]) -> list[str]:
    """The canonical seeds strictly below the head whose path to the head crosses no marked revision, oldest first."""
    selected = []
    for index, step in enumerate(line[:-1]):
        if step.revision in seeded and not any(later.marked for later in line[index + 1 :]):
            selected.append(step.revision)
    return selected
