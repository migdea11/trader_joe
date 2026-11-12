"""Every test's seed root, and the guard that no test writes the real one (validator, tj-7zjov1.297).

The dev reset writes an export (dev_seed.SEED_ROOT, output/dev_seed, by default). doubles.run_tool always hands the
tool a fresh directory under the test's own tmp_path instead (doubles.SEED_BASE, set here), so the fixed clock's stamp
can never collide between two runs and no test leaves a seed in the checkout. The guard below fails any test that
changes what is under the real seed root anyway (a direct call that forgot seed_root, or run_tool losing its default).
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from tools.dev_datasets import dev_seed
from tools.dev_datasets.tests import doubles


def _listing(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob('*')) if root.exists() else ['<absent>']


@pytest.fixture(autouse=True)
def seed_base(tmp_path: Path) -> Iterator[Path]:
    base = tmp_path / 'seed-roots'
    base.mkdir()
    doubles.SEED_BASE = base
    before = _listing(dev_seed.SEED_ROOT)
    try:
        yield base
    finally:
        doubles.SEED_BASE = None
    assert _listing(dev_seed.SEED_ROOT) == before, f'a test wrote under the real seed root {dev_seed.SEED_ROOT}'
