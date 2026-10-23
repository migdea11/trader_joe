"""Revision a6d3f8e1c205: override_action gains SET_COLLECTING (tj-grna9p.183), pinned offline.

THE DESIGN is the bead's body. UPGRADE: one statement, ALTER TYPE override_action ADD VALUE IF NOT EXISTS
'SET_COLLECTING', upper case like the two existing labels and the Python member, no backfill (the new value cannot be
used in the migration's own transaction and is not). DOWNGRADE (Postgres cannot drop an enum value): REFUSE, raising
with a message and changing NOTHING, when any dataset_override_log row holds SET_COLLECTING (the table is append-only
audit: never delete those rows to let a downgrade pass); otherwise recreate the type without the value in the
downgrade's one transaction: rename the type to override_action_old, create override_action ('SET_EXPIRY', 'DELETE'),
ALTER COLUMN action TYPE override_action USING action::text::override_action, drop override_action_old. No
additive-exception marker: the generic lossless round trip holds for every committed seed, none having such a row.

WHAT TIER THIS IS. SQLite cannot run ALTER TYPE, so the revision runs here against a recorded `op` and a scripted
bind: the exact SQL text, the ORDER of the statements, and that the refusal is decided BEFORE the first change. The
same revision on Postgres 17 (from empty, from the e5c8b1d4f703 seed, a repeat, the refusing and the lossless
downgrade, the native labels) is tests/system/test_override_action_revision.py's, which needs test mode.
"""

import importlib.util
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql

from data.store.app.database.models.dataset_override_log import DatasetOverrideLog, OverrideAction


pytestmark = pytest.mark.data_store

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / 'migrations'
REVISION = 'a6d3f8e1c205'
PARENT = 'e5c8b1d4f703'
REVISION_FILE = MIGRATIONS_DIR / 'versions' / f'{REVISION}_override_action_set_collecting.py'
OVERRIDE_LOG_FILE = MIGRATIONS_DIR / 'versions' / 'd7e2a9c35b18_dataset_override_log_and_deletion.py'
ADDITIVE_EXCEPTION_MARKER = re.compile(r'^# additive-exception: \S+', re.MULTILINE)

UPGRADE_SQL = "ALTER TYPE override_action ADD VALUE IF NOT EXISTS 'SET_COLLECTING'"
DOWNGRADE_SQL = [
    'ALTER TYPE override_action RENAME TO override_action_old',
    ('create', 'override_action', ('SET_EXPIRY', 'DELETE')),
    'ALTER TABLE dataset_override_log ALTER COLUMN action TYPE override_action USING action::text::override_action',
    'DROP TYPE override_action_old',
]


def _load(path: Path = REVISION_FILE, name: str = f'revision_{REVISION}') -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(direction: str, used: int = 0) -> tuple[list[Any], MagicMock]:
    """Run upgrade or downgrade against a recorded op; `used` is what the bind's count of SET_COLLECTING rows answers.

    Returns every change in the order it was issued (op.execute text, and ENUM.create as a tuple) and the bind.
    """
    module = _load()
    issued: list[Any] = []
    op = MagicMock(name='op')
    bind = MagicMock(name='bind')
    bind.execute.return_value.scalar_one.return_value = used
    op.get_bind.return_value = bind
    op.execute.side_effect = lambda statement: issued.append(str(statement))

    def created(self, bind_=None, checkfirst=True):
        issued.append(('create', self.name, tuple(self.enums)))

    with patch.object(module, 'op', op), patch.object(postgresql.ENUM, 'create', created):
        getattr(module, direction)()
    return issued, bind


# --------------------------------------------------------------------------------------------- the chain


def test_the_revision_is_the_head_directly_on_the_collect_revision_and_is_additive():
    script = ScriptDirectory(str(MIGRATIONS_DIR))
    assert script.get_heads() == [REVISION]
    module = _load()
    assert (module.revision, module.down_revision) == (REVISION, PARENT)
    assert not ADDITIVE_EXCEPTION_MARKER.search(REVISION_FILE.read_text(encoding='utf-8'))


def test_the_value_is_the_python_members_value_and_upper_case_like_the_existing_labels():
    module = _load()
    assert module.NEW_MEMBER == OverrideAction.SET_COLLECTING.value == OverrideAction.SET_COLLECTING.name
    assert module.NEW_MEMBER.isupper()
    previous = _load(OVERRIDE_LOG_FILE, 'revision_d7e2a9c35b18').ACTION_MEMBERS
    assert tuple(module.PREVIOUS_MEMBERS) == tuple(previous) == ('SET_EXPIRY', 'DELETE')
    assert {*previous, module.NEW_MEMBER} == {member.value for member in OverrideAction}


def test_the_type_and_table_names_are_the_models():
    module = _load()
    action = DatasetOverrideLog.__table__.c.action
    assert module.TYPE_NAME == action.type.name == 'override_action'
    assert module.TABLE == DatasetOverrideLog.TABLE_NAME == 'dataset_override_log'
    # The model maps members to labels by VALUE (values_callable): the label the revision adds is what it writes.
    assert list(action.type.enums) == [member.value for member in OverrideAction]


# --------------------------------------------------------------------------------------------- upgrade


def test_upgrade_is_exactly_one_idempotent_add_value_and_reads_nothing():
    issued, bind = _run('upgrade')
    assert issued == [UPGRADE_SQL]
    assert 'IF NOT EXISTS' in issued[0], 'a repeat upgrade would fail on the existing label'
    assert not bind.execute.called, 'the upgrade reads or backfills rows'


# --------------------------------------------------------------------------------------------- downgrade


def test_downgrade_without_a_set_collecting_row_recreates_the_type_in_order():
    issued, _bind = _run('downgrade', used=0)
    assert issued == DOWNGRADE_SQL


def test_downgrade_counts_the_rows_holding_the_value_before_anything_else():
    _issued, bind = _run('downgrade', used=0)
    ((statement, params),) = [c.args for c in bind.execute.call_args_list]
    sql = ' '.join(str(statement).split())
    assert sql == 'SELECT count(*) FROM dataset_override_log WHERE action::text = :member', sql
    assert params == {'member': 'SET_COLLECTING'}


@pytest.mark.parametrize('used', [1, 7])
def test_downgrade_with_a_set_collecting_row_refuses_and_changes_nothing(used):
    module = _load()
    op = MagicMock(name='op')
    bind = MagicMock(name='bind')
    bind.execute.return_value.scalar_one.return_value = used
    op.get_bind.return_value = bind
    with (
        patch.object(module, 'op', op),
        patch.object(postgresql.ENUM, 'create') as create,
        pytest.raises(RuntimeError) as refused,
    ):
        module.downgrade()

    message = str(refused.value)
    assert f'{used} row(s)' in message and 'SET_COLLECTING' in message and 'append-only' in message, message
    assert 'nothing was changed' in message
    assert op.execute.call_args_list == [], f'the refusing downgrade issued {op.execute.call_args_list}'
    assert not create.called, 'the refusing downgrade created a type'
    # Only the count was sent: no DELETE of the audit rows, nothing else through the bind.
    assert len(bind.execute.call_args_list) == 1
    assert all('DELETE' not in str(c.args[0]).upper() for c in bind.execute.call_args_list)


def test_no_statement_of_either_direction_deletes_or_updates_a_row():
    up, _ = _run('upgrade')
    down, bind = _run('downgrade', used=0)
    texts = [s for s in [*up, *down] if isinstance(s, str)] + [str(c.args[0]) for c in bind.execute.call_args_list]
    assert not [t for t in texts if re.search(r'\b(DELETE|UPDATE|TRUNCATE|INSERT)\b', t, re.IGNORECASE)], texts


def test_downgrade_is_the_inverse_of_upgrade_on_the_type():
    """The recreated type is exactly the pre-upgrade one: d7e2a9c35b18's labels, in its order, under the same name."""
    (create,) = [s for s in _run('downgrade', used=0)[0] if isinstance(s, tuple)]
    previous = _load(OVERRIDE_LOG_FILE, 'revision_d7e2a9c35b18')
    assert create == ('create', 'override_action', tuple(previous.ACTION_MEMBERS))


def test_the_downgrade_runs_in_the_one_migration_transaction():
    """No autocommit block or commit of its own: env.py's transaction holds the four statements together."""
    text = REVISION_FILE.read_text(encoding='utf-8')
    assert 'autocommit_block' not in text and 'COMMIT' not in text.upper().replace('COMMITTED', ''), text
