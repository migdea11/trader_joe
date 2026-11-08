"""The alpha baseline revision as test material: loaded, its upgrade() recorded offline, its schema rebuilt (ADR tj-kg521d).

No test functions here. Shared by test_alpha_baseline_revision.py (the B1-B3 checks and the offline baseline-versus-
models agreement) and test_market_activity_idempotency.py (the bar's natural key as the baseline creates it).

HOW UPGRADE RUNS WITH NO DATABASE. alembic.op is a proxy onto a live migration context, so the revision module's own
`op` is patched with a recording stand-in and upgrade() runs for real: every create_table and create_index call is kept
with the SQLAlchemy objects the revision built. Reading the revision's constants alone would pass a body that did
nothing. The enum types are created through op.get_bind(), a stand-in too, so nothing reaches a server.
"""

import ast
import importlib
import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from common.database.sql_alchemy_table import AppBase


STORE_DIR = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = STORE_DIR / 'migrations'
VERSIONS_DIR = MIGRATIONS_DIR / 'versions'
BASELINE_REVISION = '5d9a3c7e1b84'
BASELINE_FILE = VERSIONS_DIR / f'{BASELINE_REVISION}_alpha_baseline.py'
DECISION = 'tj-kg521d'


def load_baseline() -> ModuleType:
    """The baseline revision module, imported from its file under a private name."""
    spec = importlib.util.spec_from_file_location(f'alpha_baseline_{BASELINE_REVISION}', BASELINE_FILE)
    assert spec is not None and spec.loader is not None, BASELINE_FILE
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass
class RecordedUpgrade:
    """What upgrade() asked for: the op stand-in, the enum types it created, and the schema it described."""

    op: MagicMock
    created_enums: list[str]
    metadata: sa.MetaData
    index_calls: list[tuple[str, str]] = field(default_factory=list)

    @property
    def calls(self) -> list[str]:
        return [name for name, _args, _kwargs in self.op.method_calls]


def record_upgrade(module: ModuleType | None = None) -> RecordedUpgrade:
    """Run the baseline's upgrade() against a recording op and rebuild the schema it describes as a MetaData.

    create_table(name, *elements) becomes sa.Table(name, metadata, *elements); create_index(name, table, columns,
    unique=, postgresql_where=) becomes an sa.Index on that table (a string names a column, anything else is an
    expression, such as `started_at DESC`). The enum types are read off the ENUM.create calls it makes.
    """
    module = module or load_baseline()
    created: list[str] = []

    def note_create(self, bind=None, checkfirst=True):  # the real signature, bind and checkfirst unused
        created.append(self.name)

    recorder = MagicMock(name='op')
    with patch.object(module, 'op', recorder), patch.object(postgresql.ENUM, 'create', note_create):
        module.upgrade()

    metadata = sa.MetaData()
    tables: dict[str, sa.Table] = {}
    recorded = RecordedUpgrade(recorder, created, metadata)
    for name, args, kwargs in recorder.method_calls:
        if name == 'create_table':
            table_name, *elements = args
            tables[table_name] = sa.Table(table_name, metadata, *elements, **kwargs)
        elif name == 'create_index':
            index_name, table_name, columns = args
            table = tables[table_name]
            expressions = [table.c[column] if isinstance(column, str) else column for column in columns]
            options: dict[str, Any] = {'unique': kwargs.get('unique', False)}
            if kwargs.get('postgresql_where') is not None:
                options['postgresql_where'] = kwargs['postgresql_where']
            index = sa.Index(index_name, *expressions, **options)
            # Bound through its first named column; every baseline index names one, so none is left tableless.
            assert index.table is table, f'{index_name}: no named column binds it to {table_name}'
            recorded.index_calls.append((index_name, table_name))
    return recorded


def env_model_modules() -> set[str]:
    """migrations/env.py's ALLOWED_MODELS, read from its source (importing env.py needs a migration context)."""
    tree = ast.parse((MIGRATIONS_DIR / 'env.py').read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, 'id', None) == 'ALLOWED_MODELS' for t in node.targets):
            return set(ast.literal_eval(node.value))
    raise AssertionError('migrations/env.py no longer defines ALLOWED_MODELS')


def model_metadata() -> sa.MetaData:
    """The store's model metadata, as alembic sees it: every model module env.py loads, imported first."""
    for name in sorted(env_model_modules()):
        importlib.import_module(f'data.store.app.database.models.{name}')
    return AppBase.DATA_STORE_BASE.metadata
