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

AND ONE IMAGE DIRECTORY IS NOW TWO CHECKOUT DIRECTORIES (tj-iontkq.4). /code is assembled from
BOTH: the Dockerfile COPYs the four service trees out of ./server, and the compose files bind-mount
tests/fakes, tests/system and pytest.ini out of the repository root, which is also where
gen/proto/python stays. In the image those are one directory; on the host, since the move, they are
not. So a single host root no longer mirrors the image's path, and the mirror below takes both --
ROOT for the service trees, CONTEXT_ROOT for everything else. Before the move the two were the same
directory and the list collapsed to the two entries it always had, which is exactly why nothing
noticed: this file's own docstring used to record that the second entry was dormant.
"""

import os
from pathlib import Path, PurePosixPath

from common.tests.roots import REPO_ROOT, SERVER_ROOT


# The image's code root: its WORKDIR, and the first PYTHONPATH entry.
IMAGE_CODE_ROOT = PurePosixPath('/code')

# The code root as a PYTHONPATH entry names it, i.e. relative to itself.
IMAGE_CODE_ROOT_ENTRY = PurePosixPath('.')

# The image's PYTHONPATH entries relative to its code root, in its order: the root itself, so
# `import common` resolves, then the generated gRPC code's root, so `import trader_joe.proto` does.
# CONTAINER-RELATIVE AND UNCHANGED BY THE MOVE: test_ci_invariants pins every ENV PYTHONPATH in the
# Dockerfile against this tuple, and /code did not move.
IMAGE_PYTHONPATH_ENTRIES = (IMAGE_CODE_ROOT_ENTRY, PurePosixPath('gen/proto/python'))


def image_import_roots(root: Path = SERVER_ROOT, context_root: Path = REPO_ROOT) -> list[Path]:
    """The checkout directories the image's PYTHONPATH entries resolve to, in the image's order.

    ROOT is what the image calls /code as far as the service trees go -- the directory `import
    common` resolves against. CONTEXT_ROOT is the repository root, which supplies the rest of /code
    (tests/, bind-mounted) and every other PYTHONPATH entry, gen/proto/python among them. Passing
    one directory for both gives the two-entry list this returned before the trees moved, which is
    what a caller standing up a synthetic single-rooted tree wants.

    Duplicates are collapsed rather than repeated, so the pre-move and synthetic cases produce
    exactly the list they always did.
    """
    roots: list[Path] = []
    for entry in IMAGE_PYTHONPATH_ENTRIES:
        if entry == IMAGE_CODE_ROOT_ENTRY:
            roots.extend((root, context_root))
        else:
            roots.append(context_root / entry)
    return list(dict.fromkeys(roots))


def image_pythonpath(root: Path = SERVER_ROOT, context_root: Path = REPO_ROOT) -> str:
    """PYTHONPATH as the service image sets it, with its code root read as ROOT and CONTEXT_ROOT."""
    return os.pathsep.join(str(path) for path in image_import_roots(root, context_root))
