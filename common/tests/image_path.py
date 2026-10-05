"""The service image's import path, for the processes the suite starts outside pytest (decision tj-3mk3u5.42 F1).

The image sets PYTHONPATH to its code root, then the generated gRPC code's import root:
/code:/code/gen/proto/python. pytest.ini's pythonpath gives THIS process the second entry, but a child
process never reads pytest.ini. A test that starts a fresh interpreter the way a service starts -- a
uvicorn import, an alembic run, a logging probe -- hands it this path instead of the repository root
alone. Today no app imports generated code, so the root alone still passes; it stops passing the day
a servicer that imports trader_joe.proto is registered, and only the subprocess tests would notice.

This is the one place the mirror is spelled. common/tests/test_ci_invariants.py pins it equal to every
ENV PYTHONPATH in the Dockerfile, with the code root /code read as the checkout, so it cannot drift
from the image.

No test functions, and no dependency beyond the standard library and common/tests/roots.py (itself
stdlib-only): data/*/tests and tests/system import it too, the last inside the test_client
container, where the checkout root is /code.

THE ROOT IS THE SERVER ROOT, NOT THE REPOSITORY ROOT (tj-iontkq.2). What the image calls /code is
the directory `import common` resolves against -- the import root -- so SERVER_ROOT is the default
for `root` below and the right argument at every call site. REPO_ROOT would be a directory with no
common/ under it, and the subprocess would fail to import the very module it was started to import.
"""

import os
from pathlib import Path, PurePosixPath

from common.tests.roots import SERVER_ROOT


# The image's code root: its WORKDIR, and the first PYTHONPATH entry.
IMAGE_CODE_ROOT = PurePosixPath('/code')

# The image's PYTHONPATH entries relative to its code root, in its order: the root itself, so
# `import common` resolves, then the generated gRPC code's root, so `import trader_joe.proto` does.
IMAGE_PYTHONPATH_ENTRIES = (PurePosixPath('.'), PurePosixPath('gen/proto/python'))


def image_import_roots(root: Path = SERVER_ROOT) -> list[Path]:
    """The image's PYTHONPATH entries, in order, with its code root read as ROOT."""
    return [root / entry for entry in IMAGE_PYTHONPATH_ENTRIES]


def image_pythonpath(root: Path = SERVER_ROOT) -> str:
    """PYTHONPATH as the service image sets it, with its code root read as ROOT."""
    return os.pathsep.join(str(path) for path in image_import_roots(root))
