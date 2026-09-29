"""tj-ijpys9.19: migrations/env.py leaves stdout to alembic, and a missing model fails the run.

CI's Migrate Database step compares `alembic heads` with `alembic current`, taking the first field
of every non-blank stdout line (.github/workflows/trader_joe_testing.yml, the `revisions()` shell
function). `alembic current` loads env.py; `alembic heads` does not. So anything env.py writes to
stdout is read as a revision id: the four print() lines env.py used to emit turned `current` into
'Successfully Successfully Successfully Registered <rev>' and failed the step against a database
that was at head.

WHY A SUBPROCESS FOR THE STDOUT CHECK, not runpy under a faked alembic.context with capfd:
the property is about what the `alembic current` command writes to the stream CI parses, and only
the real command exercises every piece of that path -- alembic's CLI, the real alembic.ini (whose
fileConfig() routes the console handler to stderr), the real context proxy, and env.py's
run_migrations_online(). An in-process run would also inherit whatever logging handlers pytest
has installed, so a record could land on stdout in CI and not here, or the reverse. The database
URL points at a closed local port, so the run is deterministic and needs no Postgres: every line of
env.py's module scope runs, then the connect is refused at once. The test asserts on stderr that
the refusal was reached, so an env.py that stopped early -- and therefore printed nothing because
it did nothing -- cannot pass.

WHY IN-PROCESS FOR THE ImportError CHECK: forcing one model import to fail from outside a
subprocess would need a sitecustomize shim on the child's path; under runpy it is one
sys.modules entry. The property is that env.py raises instead of swallowing, and an exception out
of env.py is exactly what makes the alembic command exit non-zero. The fake context is the one
test_env_uri_masking.py documents.
"""

import runpy
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import alembic
import pytest
from alembic.config import Config


pytestmark = pytest.mark.data_store

REPO_ROOT = Path(__file__).resolve().parents[3]
STORE_DIR = REPO_ROOT / 'data' / 'store'
ENV_PY = STORE_DIR / 'migrations' / 'env.py'

# Port 1 on loopback: nothing listens there, so psycopg2 is refused immediately rather than
# waiting on a timeout. Fixture credentials, not real ones.
UNREACHABLE_DATABASE_URI = 'postgresql://joe:fixture-pass@127.0.0.1:1/unreachable'

# env.py's ALLOWED_MODELS, restated rather than imported: reading it would mean executing env.py.
ALLOWED_MODELS = ('base_market_activity', 'stock_market_activity', 'store_dataset_entry')


def alembic_current_env() -> dict[str, str]:
    """Build the whole environment `alembic current` runs under.

    A wholesale replacement, not os.environ with entries removed, so a DATABASE_URI or logging
    setting in the developer's shell cannot change the result. POSTGRES_ASYNC=true mirrors the
    data_store container CI runs the command in. PYTHONPATH puts the repo on the path, as /code
    is in the image.

    Returns:
        dict[str, str]: Every variable the child process will see.
    """
    return {'PYTHONPATH': str(REPO_ROOT), 'DATABASE_URI': UNREACHABLE_DATABASE_URI, 'POSTGRES_ASYNC': 'true'}


@pytest.fixture(scope='module')
def alembic_current() -> subprocess.CompletedProcess:
    """Run `alembic current` from data/store, as CI does from /code, against a closed port."""
    return subprocess.run(
        [sys.executable, '-m', 'alembic', 'current'],
        cwd=STORE_DIR,
        env=alembic_current_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_alembic_current_reached_the_database_connect(alembic_current):
    # The precondition for the stdout assertion below meaning anything: env.py ran its whole
    # module scope and got as far as run_migrations_online()'s connect, which was refused.
    assert alembic_current.returncode != 0, 'alembic current succeeded against a closed port'
    assert 'OperationalError' in alembic_current.stderr, alembic_current.stderr
    assert '127.0.0.1' in alembic_current.stderr, alembic_current.stderr


def test_alembic_current_writes_nothing_to_stdout(alembic_current):
    assert alembic_current.stdout == '', (
        'env.py wrote to stdout, which CI parses for the revision id:\n' + alembic_current.stdout
    )


@pytest.fixture
def fake_alembic_context(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace alembic.context with a MagicMock backed by a real, file-less Config()."""
    mock_context = MagicMock(name='alembic.context')
    mock_context.config = Config()
    mock_context.is_offline_mode.return_value = True

    monkeypatch.setitem(sys.modules, 'alembic.context', mock_context)
    monkeypatch.setattr(alembic, 'context', mock_context, raising=False)
    return mock_context


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch: pytest.MonkeyPatch):
    """env.py calls load_dotenv('.env') at import time -- never touch a real file in tests."""
    import dotenv

    monkeypatch.setattr(dotenv, 'load_dotenv', lambda *args, **kwargs: None)


@pytest.mark.parametrize('model_name', ALLOWED_MODELS)
def test_env_py_propagates_an_import_error_of_an_allowed_model(
    model_name: str, fake_alembic_context: MagicMock, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture
):
    module = f'data.store.app.database.models.{model_name}'
    # None in sys.modules makes the next import of that name raise ModuleNotFoundError, an
    # ImportError, whether or not an earlier test had already imported it.
    monkeypatch.setitem(sys.modules, module, None)
    monkeypatch.setenv('DATABASE_URI', UNREACHABLE_DATABASE_URI)

    with pytest.raises(ImportError) as excinfo:
        runpy.run_path(str(ENV_PY), run_name='__env_py_under_test__')

    assert module in str(excinfo.value)
    # Raised before any migration ran against the incomplete target_metadata.
    fake_alembic_context.configure.assert_not_called()
    fake_alembic_context.run_migrations.assert_not_called()
    assert capfd.readouterr().out == ''


def test_env_py_runs_through_when_every_model_imports(
    fake_alembic_context: MagicMock, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture
):
    # The control for the parametrized test above: the same harness with no import sabotaged
    # completes, so the ImportError there came from the sabotaged model and nothing else.
    monkeypatch.setenv('DATABASE_URI', UNREACHABLE_DATABASE_URI)

    runpy.run_path(str(ENV_PY), run_name='__env_py_under_test__')

    fake_alembic_context.run_migrations.assert_called_once()
    assert capfd.readouterr().out == ''
