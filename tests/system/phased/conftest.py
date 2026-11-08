"""The phased P26 scenarios' guard: a phase test runs only when its file is named on the command line (tj-7zjov1.286).

Phase files are phase_*.py, which pytest's python_files (test_*.py) never matches, so a run of tests/system (the whole
tier) recurses into this directory and collects nothing from it. This hook is the second half: if a test of this
directory was collected although its file was not named (a file renamed to test_*.py, or python_files widened), the
collection fails here, naming the file, instead of the scenario running out of order inside the tier, where its phase
A would leave work in ingest that no orchestrator step follows up. scenario.py says why and how.
"""

from pathlib import Path

import pytest


PHASED_DIR = Path(__file__).resolve().parent


def _named_files(config: pytest.Config) -> set[Path]:
    base = Path(config.invocation_params.dir)
    return {(base / arg.split('::', 1)[0]).resolve() for arg in config.args}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    named = _named_files(config)
    strays = sorted(
        {
            str(item.path)
            for item in items
            if item.path.resolve().parent == PHASED_DIR and item.path.resolve() not in named
        }
    )
    if strays:
        raise pytest.UsageError(
            f'phased scenario file(s) collected without being named: {strays}. A phase runs only when its file is '
            'named on the command line, in the order of the step list (tests/system/phased/scenario.py).'
        )
