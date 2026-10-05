"""The one thing a bare `pytest` has to be told: the generated protobuf tree is built, not checked in.

Nothing under ``gen/`` is committed (user ruling 2026-10-05, reversing ADR tj-8konfu D3; the
addendum on that record carries the reversal). ``pytest.ini`` puts ``gen/proto/python`` on the
import path and a great deal of the suite reaches ``trader_joe.proto`` through ``common.rpc``, so on
a fresh clone that has not generated yet the run dies in collection with ``ModuleNotFoundError: No
module named 'trader_joe'`` -- scattered over dozens of files, and reading exactly like a broken
repository rather than a missing build step.

``make test`` cannot hit that: it takes ``proto`` as a prerequisite. This file is for the person who
runs ``pytest`` directly, which is a perfectly reasonable thing to do and is what CI does too. It
turns that pile into one line naming the target to run.

``pytest.UsageError`` on purpose: pytest prints it as a single ``ERROR:`` line with no traceback and
exits 4, so the message is the whole output. A conftest that generated the tree itself was rejected
-- ``gen/proto/python`` reaches Python by CONFIGURATION and never by code (decision tj-3mk3u5.42 F1
rule 3), and a test session that silently rewrote source files would be a worse surprise than a
refusal.
"""

from pathlib import Path

import pytest


GENERATED_PACKAGE = Path(__file__).parent / 'gen' / 'proto' / 'python' / 'trader_joe' / 'proto'


def pytest_configure(config: pytest.Config) -> None:
    """Refuse the session, with the target to run, when the generated package is absent or empty."""
    del config
    if any(GENERATED_PACKAGE.rglob('*_pb2.py')):
        return
    raise pytest.UsageError(
        f'{GENERATED_PACKAGE.relative_to(Path(__file__).parent)} holds no generated modules. '
        'The protobuf code is generated from proto/ and is never committed, so a fresh clone has '
        "none of it yet -- this is not a broken checkout. Run 'make proto' (or 'make test', which "
        'generates first), then run pytest again.'
    )
