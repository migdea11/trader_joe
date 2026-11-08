"""The migration additivity gate, tools.migration_lint (tj-7zjov1.8, folding tj-aw0tuk).

Every case is a revision written as source and handed to the production lint_source, or files on disk
handed to main; nothing here re-states the rules. A rule is pinned on both sides: what it refuses in
upgrade(), and what it lets through (downgrade(), a server default, a nullable column, additive SQL).
"""

import re
from pathlib import Path

import pytest

from tools.migration_lint import FIX_HINT, VERSIONS_DIR, Finding, lint_source, main, marker_of, revision_files


HEADER = 'from alembic import op\nimport sqlalchemy as sa\n\n'
# The one revision in the tree since the alpha baseline (ADR tj-kg521d): create-only, unmarked.
BASELINE_FILE = '5d9a3c7e1b84_alpha_baseline.py'


def _revision(upgrade: str = 'pass', downgrade: str = 'pass', *, prelude: str = '') -> str:
    """A revision module: PRELUDE at module level, then upgrade() and downgrade() with the given bodies."""
    return f'{HEADER}{prelude}\n\ndef upgrade():\n    {upgrade}\n\n\ndef downgrade():\n    {downgrade}\n'


def _messages(source: str) -> list[str]:
    return [finding.message for finding in lint_source(Path('rev.py'), source)]


def _strip_marker(source: str) -> str:
    return re.sub(r'^# additive-exception: .*$', '', source, flags=re.MULTILINE)


# --- the always-refused operations -----------------------------------------------------------------

DESTRUCTIVE_OPS = {
    'drop_column': "op.drop_column('bar', 'close')",
    'drop_table': "op.drop_table('bar')",
    'drop_constraint': "op.drop_constraint('bar_key', 'bar', type_='unique')",
    'rename_table': "op.rename_table('bar', 'bars')",
}


@pytest.mark.parametrize('statement', DESTRUCTIVE_OPS.values(), ids=DESTRUCTIVE_OPS.keys())
def test_an_unmarked_destructive_op_in_upgrade_is_refused(statement: str):
    source = _revision(upgrade=statement)
    findings = lint_source(Path('rev.py'), source)
    assert len(findings) == 1, findings
    assert findings[0].message.startswith(statement.split('(')[0].removeprefix('op.'))
    expected_line = source.splitlines().index(f'    {statement}') + 1
    assert findings[0].line == expected_line, 'the finding must point at the offending line of the revision'
    assert findings[0].message.endswith(FIX_HINT)


@pytest.mark.parametrize('statement', DESTRUCTIVE_OPS.values(), ids=DESTRUCTIVE_OPS.keys())
def test_the_same_op_in_downgrade_passes(statement: str):
    """downgrade() is exempt: the inverse of an additive step is a drop by construction."""
    assert _messages(_revision(downgrade=statement)) == []


def test_a_downgrade_exemption_does_not_leak_into_upgrade():
    source = _revision(upgrade=DESTRUCTIVE_OPS['drop_column'], downgrade=DESTRUCTIVE_OPS['drop_table'])
    assert [m.split(' ')[0] for m in _messages(source)] == ['drop_column']


def test_a_batch_op_drop_is_refused_too():
    source = _revision(upgrade="with op.batch_alter_table('bar') as batch:\n        batch.drop_column('close')")
    assert len(_messages(source)) == 1


# --- drop_index: autogenerate emits it whenever an index leaves the models (architect RE:, round 2) ---

DROP_INDEX = "op.drop_index('ix_bar_key', table_name='bar')"
BATCH_DROP_INDEX = "with op.batch_alter_table('bar') as b:\n        b.drop_index('ix_bar_key')"


def test_an_unmarked_drop_index_in_upgrade_is_refused_with_its_reason_and_the_fix_hint():
    """Dropping a unique index an ON CONFLICT upsert names makes old-code writes fail while reads pass."""
    source = _revision(upgrade=DROP_INDEX)
    findings = lint_source(Path('rev.py'), source)
    assert len(findings) == 1, findings
    assert findings[0].message == f'drop_index drops an index; {FIX_HINT}'
    assert findings[0].line == source.splitlines().index(f'    {DROP_INDEX}') + 1


def test_the_batch_form_of_drop_index_in_upgrade_is_refused():
    source = _revision(upgrade=BATCH_DROP_INDEX)
    findings = lint_source(Path('rev.py'), source)
    assert [f.message for f in findings] == [f'drop_index drops an index; {FIX_HINT}']
    assert findings[0].line == source.splitlines().index("        b.drop_index('ix_bar_key')") + 1


@pytest.mark.parametrize('statement', [DROP_INDEX, BATCH_DROP_INDEX], ids=['op', 'batch'])
def test_drop_index_in_downgrade_passes(statement: str):
    """The inverse of create_index: every unmarked real revision drops its indexes this way."""
    assert _messages(_revision(downgrade=statement)) == []


@pytest.mark.parametrize('statement', [DROP_INDEX, BATCH_DROP_INDEX], ids=['op', 'batch'])
def test_drop_index_in_upgrade_of_a_marked_revision_passes(statement: str):
    source = _revision(upgrade=statement, prelude='# additive-exception: tj-abc123')
    assert lint_source(Path('rev.py'), source) == []


def test_a_marked_revision_that_drops_indexes_is_refused_for_each_without_its_marker():
    """Three drop_index calls in a marked upgrade(): passed marked, and with the marker stripped the gate names each.

    Synthetic since the alpha baseline (ADR tj-kg521d): the marked revision that once dropped three indexes is gone
    with the old chain, and the shape it had is written out here instead.
    """
    drops = '\n    '.join(f"op.drop_index('ix_bar_{name}', table_name='bar')" for name in ('a', 'b', 'c'))
    source = _revision(upgrade=drops, prelude='# additive-exception: tj-abc123')
    assert lint_source(Path('rev.py'), source) == []
    found = [f for f in lint_source(Path('rev.py'), _strip_marker(source)) if f.message.startswith('drop_index')]
    assert len(found) == 3, found


# --- alter_column ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('keyword', 'named'),
    [
        ('type_=sa.SmallInteger()', 'type_'),
        ("new_column_name='closing'", 'new_column_name'),
        ('nullable=False', 'nullable=False'),
    ],
    ids=['type', 'rename', 'not-null'],
)
def test_alter_column_that_retypes_renames_or_sets_not_null_is_refused(keyword: str, named: str):
    messages = _messages(_revision(upgrade=f"op.alter_column('bar', 'close', {keyword})"))
    assert len(messages) == 1 and named in messages[0], messages


@pytest.mark.parametrize(
    'keyword', ["server_default='0'", 'nullable=True'], ids=['server-default-alone', 'relax-to-nullable']
)
def test_alter_column_that_only_sets_a_default_or_relaxes_nullability_passes(keyword: str):
    assert _messages(_revision(upgrade=f"op.alter_column('bar', 'close', {keyword})")) == []


def test_alter_column_reports_each_breach_it_carries():
    statement = "op.alter_column('bar', 'close', type_=sa.Integer(), nullable=False)"
    assert len(_messages(_revision(upgrade=statement))) == 2


def test_alter_column_in_downgrade_passes():
    assert _messages(_revision(downgrade="op.alter_column('bar', 'close', type_=sa.Integer())")) == []


# --- add_column: the sneaky breaker ----------------------------------------------------------------


@pytest.mark.parametrize(
    'column',
    ["sa.Column('feed', sa.Integer(), nullable=False)", "sa.Column('id', sa.Integer(), primary_key=True)"],
    ids=['nullable-false', 'primary-key'],
)
def test_add_column_not_null_without_a_server_default_is_refused(column: str):
    """tj-aw0tuk: it looks additive, and makes every write fail while reads still pass."""
    messages = _messages(_revision(upgrade=f"op.add_column('bar', {column})"))
    assert len(messages) == 1 and 'NOT NULL' in messages[0] and 'server_default' in messages[0], messages


def test_add_column_with_only_a_python_side_default_is_still_refused():
    """default= is applied by SQLAlchemy in the new code; old code inserting without it still breaks."""
    column = "sa.Column('feed', sa.Integer(), nullable=False, default=1)"
    assert len(_messages(_revision(upgrade=f"op.add_column('bar', {column})"))) == 1


@pytest.mark.parametrize(
    'column',
    [
        "sa.Column('feed', sa.Integer(), nullable=False, server_default='1')",
        "sa.Column('feed', sa.Integer(), nullable=True)",
        "sa.Column('feed', sa.Integer())",
    ],
    ids=['not-null-with-server-default', 'nullable', 'nullable-by-default'],
)
def test_add_column_with_a_server_default_or_nullable_passes(column: str):
    assert _messages(_revision(upgrade=f"op.add_column('bar', {column})")) == []


@pytest.mark.parametrize(
    'column',
    [
        "sa.Column('feed', sa.Integer(), nullable=False, server_default=None)",
        "sa.Column('id', sa.Integer(), primary_key=True, server_default=None)",
    ],
    ids=['nullable-false', 'primary-key'],
)
def test_add_column_not_null_with_an_explicit_none_server_default_is_refused(column: str):
    """server_default=None is no default: old code inserting without the column still breaks."""
    source = _revision(upgrade=f"op.add_column('bar', {column})")
    findings = lint_source(Path('rev.py'), source)
    assert [f.message for f in findings] == [f'add_column of a NOT NULL column without a server_default; {FIX_HINT}']


@pytest.mark.parametrize(
    'column',
    [
        "sa.Column('feed', sa.Text(), nullable=False, server_default='x')",
        "sa.Column('id', sa.Text(), primary_key=True, server_default='x')",
        "sa.Column('feed', sa.Text(), nullable=False, server_default='')",
        "sa.Column('feed', sa.Integer(), nullable=False, server_default=sa.text('0'))",
    ],
    ids=['nullable-false', 'primary-key', 'empty-string', 'sql-expression'],
)
def test_add_column_not_null_with_a_real_server_default_still_passes(column: str):
    """Positive control for the None rule: only the None constant counts as absent, not every falsy value."""
    assert _messages(_revision(upgrade=f"op.add_column('bar', {column})")) == []


def test_add_column_column_passed_by_keyword_is_still_read():
    statement = "op.add_column('bar', column=sa.Column('feed', sa.Integer(), nullable=False))"
    assert len(_messages(_revision(upgrade=statement))) == 1


# --- raw SQL ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ('upgrade', 'prelude'),
    [
        ("op.execute('DELETE FROM bar')", ''),
        ("op.execute(f'DROP TABLE {TABLE}')", "TABLE = 'bar'"),
        ("op.execute(sa.text('TRUNCATE bar'))", ''),
        ("op.execute('ALTER TABLE bar ALTER COLUMN close SET NOT NULL')", ''),
        ("op.execute('ALTER TABLE bar RENAME TO bars')", ''),
        ('op.execute(STATEMENT)', "STATEMENT = 'drop ' + 'table bar'"),
        ("op.get_bind().execute(sa.text('DELETE FROM bar'))", ''),
    ],
    ids=['delete', 'f-string-drop-table', 'text-truncate', 'set-not-null', 'rename', 'module-concat', 'bind-execute'],
)
def test_destructive_raw_sql_in_upgrade_is_refused(upgrade: str, prelude: str):
    """tj-aw0tuk's raw-SQL hole: 8f41c2d7a3b9 deleted rows through op.execute, which a token scan missed."""
    messages = _messages(_revision(upgrade=upgrade, prelude=prelude))
    assert len(messages) == 1 and messages[0].startswith('raw SQL contains'), messages


@pytest.mark.parametrize(
    'upgrade',
    ['op.execute(statement_from_somewhere())', 'op.execute(UNDEFINED)', 'op.execute(sa.text(UNDEFINED))'],
    ids=['call-result', 'unresolved-name', 'text-of-unresolved-name'],
)
def test_raw_sql_the_lint_cannot_read_is_refused(upgrade: str):
    """A gate that passes what it cannot see is the hole tj-aw0tuk was filed to close."""
    messages = _messages(_revision(upgrade=upgrade))
    assert len(messages) == 1 and 'cannot read' in messages[0], messages


@pytest.mark.parametrize(
    'upgrade',
    [
        "stmt = statement_from_somewhere()\n    op.execute(f'{stmt}')",
        "op.execute(f'{verb} TABLE bar')",
        "op.execute(f'UPDATE bar SET c = {value}')",
        "op.execute(f'UPDATE {sa.something} SET c = 1')",
    ],
    ids=['whole-statement-interpolated', 'verb-interpolated', 'additive-text-unresolved-tail', 'attribute-part'],
)
def test_an_f_string_with_a_part_the_lint_cannot_resolve_is_refused(upgrade: str):
    """One unresolvable part makes the whole statement unreadable, however additive the readable rest is."""
    messages = _messages(_revision(upgrade=upgrade))
    assert len(messages) == 1 and 'cannot read' in messages[0], messages


@pytest.mark.parametrize(
    ('prelude', 'value'),
    [('N = 1', '1'), ('N = 1.5', '1.5'), ("N = OTHER\nOTHER = 'bar'", 'bar')],
    ids=['int-constant', 'float-constant', 'name-chain-to-str'],
)
def test_an_f_string_part_resolving_to_a_module_constant_is_read(prelude: str, value: str):
    """Resolved parts are read as text, not skipped: the resolved value reaches the SQL checks."""
    additive = _revision(upgrade="op.execute(f'UPDATE bar SET c = {N}')", prelude=prelude)
    assert _messages(additive) == []
    refused = _messages(_revision(upgrade="op.execute(f'DELETE FROM bar WHERE c = {N}')", prelude=prelude))
    assert len(refused) == 1 and refused[0].startswith('raw SQL contains DELETE FROM'), refused


def test_an_f_string_part_resolving_to_a_destructive_verb_is_refused():
    messages = _messages(_revision(upgrade="op.execute(f'{VERB} TABLE bar')", prelude="VERB = 'DROP'"))
    assert len(messages) == 1 and messages[0].startswith('raw SQL contains DROP TABLE'), messages


def test_raw_sql_adding_a_not_null_column_without_a_default_is_refused():
    messages = _messages(_revision(upgrade="op.execute('ALTER TABLE bar ADD COLUMN feed integer NOT NULL')"))
    assert len(messages) == 1 and messages[0].startswith('raw SQL adds a NOT NULL column without a DEFAULT'), messages


def test_raw_sql_adding_a_not_null_column_with_a_default_passes():
    """The other side of the test above, so a fix that refuses every NOT NULL is caught too."""
    upgrade = "op.execute('ALTER TABLE bar ADD COLUMN feed integer NOT NULL DEFAULT 1')"
    assert _messages(_revision(upgrade=upgrade)) == []


@pytest.mark.parametrize(
    ('upgrade', 'prelude'),
    [
        ('op.execute(f"ALTER TYPE {TYPE_NAME} ADD VALUE IF NOT EXISTS \'collecting\'")', "TYPE_NAME = 'action'"),
        ("op.execute(f'UPDATE {TABLE} SET feed = 1 WHERE feed IS NULL')", "TABLE = 'bar'"),
        ("op.execute('CREATE INDEX bar_ts ON bar (ts)')", ''),
        ("op.execute(f'UPDATE {TABLE} SET c = 1 WHERE t = {STATIC}')", "TABLE = 'bar'\nSTATIC = 1"),
    ],
    ids=['alter-type-add-value', 'update-backfill', 'create-index', 'int-constant-interpolated'],
)
def test_additive_raw_sql_passes(upgrade: str, prelude: str):
    """The builder's narrowing: a6d3f8e1c205 and e5c8b1d4f703 are unmarked and use exactly these forms."""
    assert _messages(_revision(upgrade=upgrade, prelude=prelude)) == []


NOT_ADDITIVE = 'raw SQL is not a recognised additive statement'


@pytest.mark.parametrize(
    'statement',
    [
        'SELECT 1',
        'WITH moved AS (SELECT 1) INSERT INTO bar SELECT * FROM moved',
        "DO 'BEGIN NULL; END'",
        'ALTER TABLE bar ADD CONSTRAINT bar_key UNIQUE (ts)',
        'ALTER TABLE bar DISABLE TRIGGER ALL',
        'GRANT SELECT ON bar TO someone',
        '-- backfill\nSELECT 1',
    ],
    ids=['select', 'with', 'do', 'add-constraint', 'unlisted-verb', 'grant', 'leading-comment-before-select'],
)
def test_raw_sql_not_opening_as_an_additive_form_is_refused(statement: str):
    """The allowlist: a verb nobody thought to deny is refused by omission, not passed by it."""
    messages = _messages(_revision(upgrade=f'op.execute({statement!r})'))
    assert messages and all(m.startswith(NOT_ADDITIVE) for m in messages), messages


@pytest.mark.parametrize(
    'statement',
    [
        'UPDATE bar SET c = 1',
        'INSERT INTO bar (c) VALUES (1)',
        'CREATE TABLE bar_archive (LIKE bar INCLUDING DEFAULTS)',
        'CREATE INDEX bar_ts ON bar (ts)',
        "ALTER TYPE action ADD VALUE 'collecting'",
        'ALTER TABLE bar ADD COLUMN feed integer',
        '\n    update bar set c = 1\n',
        '-- backfill\nUPDATE bar SET c = 1',
        '/* backfill */ UPDATE bar SET c = 1',
    ],
    ids=[
        'update',
        'insert-into',
        'create-table',
        'create-index',
        'alter-type-add-value',
        'add-column',
        'lower-indented',
        'leading-line-comment',
        'leading-block-comment',
    ],
)
def test_each_listed_additive_opener_passes(statement: str):
    assert _messages(_revision(upgrade=f'op.execute({statement!r})')) == []


@pytest.mark.parametrize(
    ('statement', 'expected'),
    [
        ('UPDATE bar SET c = 1; INSERT INTO bar (c) VALUES (2);', []),
        ('UPDATE bar SET c = 1; SELECT 1', [NOT_ADDITIVE]),
        ('SELECT 1; SELECT 2', [NOT_ADDITIVE, NOT_ADDITIVE]),
        (
            'ALTER TABLE bar ADD COLUMN a integer DEFAULT 0; ALTER TABLE bar ADD COLUMN b integer NOT NULL',
            ['raw SQL adds a NOT NULL column without a DEFAULT'],
        ),
    ],
    ids=['all-additive', 'additive-then-select', 'one-finding-per-statement', 'default-does-not-cross-statements'],
)
def test_a_multi_statement_string_is_judged_per_statement(statement: str, expected: list[str]):
    messages = _messages(_revision(upgrade=f'op.execute({statement!r})'))
    assert len(messages) == len(expected), messages
    assert all(m.startswith(e) for m, e in zip(messages, expected, strict=True)), messages


@pytest.mark.parametrize(
    ('call', 'expected'),
    [
        ("op.get_bind().exec_driver_sql('DELETE FROM bar')", 'raw SQL contains DELETE FROM'),
        ('op.get_bind().exec_driver_sql(UNDEFINED)', 'raw SQL the lint cannot read statically'),
        ("op.get_bind().exec_driver_sql(f'{stmt}')", 'raw SQL the lint cannot read statically'),
        ("op.get_bind().exec_driver_sql('SELECT 1')", NOT_ADDITIVE),
        (
            "op.get_bind().exec_driver_sql('ALTER TABLE bar ADD COLUMN feed integer NOT NULL')",
            'raw SQL adds a NOT NULL column without a DEFAULT',
        ),
    ],
    ids=['destructive', 'unreadable', 'unreadable-f-string', 'not-additive', 'not-null-add'],
)
def test_exec_driver_sql_is_inspected_like_execute(call: str, expected: str):
    messages = _messages(_revision(upgrade=call))
    assert len(messages) == 1 and messages[0].startswith(expected), messages


def test_additive_exec_driver_sql_passes_and_is_exempt_in_downgrade():
    assert _messages(_revision(upgrade="op.get_bind().exec_driver_sql('UPDATE bar SET c = 1')")) == []
    assert _messages(_revision(downgrade="op.get_bind().exec_driver_sql('DROP TABLE bar')")) == []


def test_destructive_raw_sql_in_downgrade_passes():
    assert _messages(_revision(downgrade="op.execute('DROP TABLE bar')")) == []


# --- raw ALTER TABLE per clause, CREATE OR REPLACE, server default removal (tj-7zjov1.42) ------------

NOT_NULL_NO_DEFAULT = f'raw SQL adds a NOT NULL column without a DEFAULT; {FIX_HINT}'
NOT_ADD_COLUMN = f'raw SQL ALTER TABLE has a clause that is not ADD COLUMN; {FIX_HINT}'
REPLACES = f'raw SQL contains CREATE OR REPLACE; {FIX_HINT}'
REMOVES_DEFAULT = f'alter_column removes the server_default of a NOT NULL column; {FIX_HINT}'

PER_CLAUSE_REFUSED = {
    'not-null-covered-by-a-later-default': (
        'ALTER TABLE bar ADD COLUMN a integer NOT NULL, ADD COLUMN b integer DEFAULT 0',
        [NOT_NULL_NO_DEFAULT],
    ),
    'not-null-in-a-later-clause': (
        'ALTER TABLE bar ADD COLUMN b integer DEFAULT 0, ADD COLUMN a integer NOT NULL',
        [NOT_NULL_NO_DEFAULT],
    ),
    'add-constraint-after-add-column': (
        'ALTER TABLE bar ADD COLUMN a integer, ADD CONSTRAINT k UNIQUE (a)',
        [NOT_ADD_COLUMN],
    ),
    'non-add-clause-lower-case': ('alter table bar add column a integer, disable trigger all', [NOT_ADD_COLUMN]),
    'one-finding-per-bad-clause': (
        'ALTER TABLE bar ADD COLUMN a integer NOT NULL, ADD CONSTRAINT k UNIQUE (a), ADD COLUMN c text NOT NULL',
        [NOT_NULL_NO_DEFAULT, NOT_ADD_COLUMN, NOT_NULL_NO_DEFAULT],
    ),
}


@pytest.mark.parametrize(('statement', 'expected'), PER_CLAUSE_REFUSED.values(), ids=PER_CLAUSE_REFUSED.keys())
def test_a_raw_alter_table_is_judged_per_clause(statement: str, expected: list[str]):
    """A DEFAULT in one clause no longer covers a NOT NULL in another, and every clause must be ADD COLUMN."""
    source = _revision(upgrade=f'op.execute({statement!r})')
    findings = lint_source(Path('rev.py'), source)
    assert [f.message for f in findings] == expected
    assert {f.line for f in findings} == {source.splitlines().index(f'    op.execute({statement!r})') + 1}


@pytest.mark.parametrize(
    'statement',
    [
        'ALTER TABLE bar ADD COLUMN a integer NOT NULL DEFAULT 1, ADD COLUMN b integer NOT NULL DEFAULT 0',
        'ALTER TABLE bar ADD COLUMN a integer, ADD COLUMN b text',
        "ALTER TABLE bar ADD COLUMN a text NOT NULL DEFAULT 'x,y', ADD COLUMN b numeric(10, 2)",
        'ALTER TABLE bar ADD COLUMN a numeric NOT NULL DEFAULT (round(1.25, 1)), ADD COLUMN b integer',
        'ALTER TABLE bar ADD COLUMN "a,b" integer NOT NULL DEFAULT 0, ADD COLUMN c integer',
        "ALTER TABLE bar ADD COLUMN a text NOT NULL DEFAULT 'it''s, fine', ADD COLUMN b integer",
        'alter table bar add column a integer not null default 0,\n    add column b integer',
    ],
    ids=[
        'default-on-each-not-null',
        'two-plain-add-columns',
        'comma-in-quotes-and-in-type-parens',
        'comma-in-a-parenthesised-default',
        'comma-in-a-quoted-identifier',
        'comma-after-an-escaped-quote',
        'lower-case-across-lines',
    ],
)
def test_a_raw_alter_table_of_additive_clauses_passes(statement: str):
    """The other side: a split inside parentheses or quotes would make a fragment that is not ADD COLUMN."""
    assert _messages(_revision(upgrade=f'op.execute({statement!r})')) == []


def test_a_not_null_column_needs_its_default_in_its_own_clause_even_with_a_typed_parens_clause():
    statement = 'ALTER TABLE bar ADD COLUMN a numeric(10, 2) NOT NULL, ADD COLUMN b integer DEFAULT 0'
    assert _messages(_revision(upgrade=f'op.execute({statement!r})')) == [NOT_NULL_NO_DEFAULT]


CREATE_OR_REPLACE = {
    'view': 'CREATE OR REPLACE VIEW bar_v AS SELECT ts FROM bar',
    'function': "CREATE OR REPLACE FUNCTION f() RETURNS integer AS 'SELECT 1' LANGUAGE sql",
    'trigger': 'CREATE OR REPLACE TRIGGER t BEFORE INSERT ON bar FOR EACH ROW EXECUTE FUNCTION f()',
    'lower-case': 'create or replace view bar_v as select ts from bar',
    'mixed-case-and-spacing': 'Create Or\n  Replace View bar_v AS SELECT ts FROM bar',
    'after-an-additive-statement': 'UPDATE bar SET c = 1; CREATE OR REPLACE VIEW bar_v AS SELECT ts FROM bar',
}


@pytest.mark.parametrize('statement', CREATE_OR_REPLACE.values(), ids=CREATE_OR_REPLACE.keys())
def test_create_or_replace_is_refused(statement: str):
    """It passes the CREATE opener yet can change a definition old code reads."""
    assert _messages(_revision(upgrade=f'op.execute({statement!r})')) == [REPLACES]


@pytest.mark.parametrize(
    'statement',
    [
        'CREATE VIEW bar_v AS SELECT ts FROM bar',
        "CREATE FUNCTION f() RETURNS integer AS 'SELECT 1' LANGUAGE sql",
        'create view bar_v as select ts from bar',
    ],
    ids=['view', 'function', 'lower-case-view'],
)
def test_a_plain_create_still_passes(statement: str):
    assert _messages(_revision(upgrade=f'op.execute({statement!r})')) == []


ALTER_DEFAULT_REFUSED = {
    'existing-nullable-false': (
        "op.alter_column('bar', 'close', server_default=None, existing_nullable=False)",
        [REMOVES_DEFAULT],
    ),
    'nullable-false-gives-both': (
        "op.alter_column('bar', 'close', server_default=None, nullable=False)",
        [f'alter_column sets nullable=False; {FIX_HINT}', REMOVES_DEFAULT],
    ),
    'batch-existing-nullable-false': (
        "with op.batch_alter_table('bar') as b:\n        b.alter_column('close', server_default=None, existing_nullable=False)",
        [REMOVES_DEFAULT],
    ),
}


@pytest.mark.parametrize(('statement', 'expected'), ALTER_DEFAULT_REFUSED.values(), ids=ALTER_DEFAULT_REFUSED.keys())
def test_removing_the_server_default_of_a_not_null_column_is_refused(statement: str, expected: list[str]):
    """Ruling (b): old-code writes that omit the column fail once its default is gone."""
    source = _revision(upgrade=statement)
    findings = lint_source(Path('rev.py'), source)
    assert [f.message for f in findings] == expected
    call_line = next(i for i, text in enumerate(source.splitlines(), 1) if 'alter_column(' in text)
    assert {f.line for f in findings} == {call_line}


@pytest.mark.parametrize(
    'statement',
    [
        "op.alter_column('bar', 'close', server_default=None, existing_nullable=True)",
        "op.alter_column('bar', 'close', server_default=None)",
        "op.alter_column('bar', 'close', server_default=None, nullable=True)",
        "op.alter_column('bar', 'close', server_default='0', existing_nullable=False)",
        "op.alter_column('bar', 'close', existing_nullable=False)",
    ],
    ids=[
        'existing-nullable-true',
        'no-nullability-keyword',
        'nullable-true',
        'real-default-on-not-null',
        'no-server-default-keyword',
    ],
)
def test_removing_a_default_the_lint_cannot_tie_to_not_null_passes(statement: str):
    assert _messages(_revision(upgrade=statement)) == []


TJ_7ZJOV1_42_REFUSALS = {
    **{f'clause-{k}': f'op.execute({v[0]!r})' for k, v in PER_CLAUSE_REFUSED.items()},
    **{f'replace-{k}': f'op.execute({v!r})' for k, v in CREATE_OR_REPLACE.items()},
    **{f'default-{k}': v[0] for k, v in ALTER_DEFAULT_REFUSED.items()},
}


@pytest.mark.parametrize('statement', TJ_7ZJOV1_42_REFUSALS.values(), ids=TJ_7ZJOV1_42_REFUSALS.keys())
def test_each_new_refusal_passes_when_marked_or_in_downgrade(statement: str):
    assert _messages(_revision(upgrade=statement)), 'precondition: refused unmarked in upgrade'
    assert _messages(_revision(upgrade=statement, prelude='# additive-exception: tj-abc123')) == []
    assert _messages(_revision(downgrade=statement)) == []


# --- SQL comments are stripped before raw SQL is split (tj-7zjov1.44) ------------------------------------

UNTERMINATED = f'raw SQL has an unterminated comment or quote; {FIX_HINT}'
DROPS_TABLE = f'raw SQL contains DROP TABLE; {FIX_HINT}'

COMMENT_REFUSED = {
    # The hole the bead was filed for: the apostrophe opened a quote and swallowed the later comma.
    'apostrophe-in-a-line-comment-hides-no-clause': (
        "ALTER TABLE bar ADD COLUMN a integer DEFAULT 0 -- don't\n, ADD CONSTRAINT k UNIQUE (a)",
        [NOT_ADD_COLUMN],
    ),
    'apostrophe-in-a-block-comment-hides-no-clause': (
        "ALTER TABLE bar ADD COLUMN a integer DEFAULT 0 /* don't */, ADD CONSTRAINT k UNIQUE (a)",
        [NOT_ADD_COLUMN],
    ),
    'default-in-a-line-comment-covers-nothing': (
        'ALTER TABLE bar ADD COLUMN a integer NOT NULL -- DEFAULT 0',
        [NOT_NULL_NO_DEFAULT],
    ),
    'default-in-a-block-comment-covers-nothing': (
        'ALTER TABLE bar ADD COLUMN a integer NOT NULL /* DEFAULT 0 */',
        [NOT_NULL_NO_DEFAULT],
    ),
    'a-line-comment-ends-at-the-newline': (
        'UPDATE bar SET c = 1 -- note\n; SELECT 1',
        [f'{NOT_ADDITIVE} (UPDATE, INSERT, CREATE, ADD VALUE, ADD COLUMN); {FIX_HINT}'],
    ),
    'unterminated-block-comment': ('ALTER TABLE bar ADD COLUMN a int /* open', [UNTERMINATED]),
    'unterminated-block-comment-hiding-a-clause': (
        'ALTER TABLE bar ADD COLUMN a int /* , ADD CONSTRAINT k UNIQUE (a)',
        [UNTERMINATED],
    ),
    'unterminated-quote': ("ALTER TABLE bar ADD COLUMN a text DEFAULT 'open", [UNTERMINATED]),
    'unterminated-quote-hiding-a-clause': (
        "ALTER TABLE bar ADD COLUMN a text DEFAULT 'open, ADD CONSTRAINT k UNIQUE (a)",
        [UNTERMINATED],
    ),
    'unterminated-identifier-quote': ('ALTER TABLE bar ADD COLUMN "open integer', [UNTERMINATED]),
    # The denylist reads the original text, comments included: the conservative reading.
    'destructive-word-in-a-line-comment': ('UPDATE bar SET c = 1 -- DROP TABLE x', [DROPS_TABLE]),
    'destructive-word-in-a-block-comment': ('UPDATE bar SET c = 1 /* DROP TABLE x */', [DROPS_TABLE]),
}


@pytest.mark.parametrize(('statement', 'expected'), COMMENT_REFUSED.values(), ids=COMMENT_REFUSED.keys())
def test_a_comment_cannot_hide_a_clause_or_supply_a_default(statement: str, expected: list[str]):
    """A comment is removed before the split, never read as SQL; what cannot be delimited is refused."""
    assert _messages(_revision(upgrade=f'op.execute({statement!r})')) == expected


@pytest.mark.parametrize(
    'statement',
    [
        "ALTER TABLE bar ADD COLUMN a integer -- don't\n, ADD COLUMN b integer",
        'ALTER TABLE bar ADD COLUMN a integer -- a, b\n, ADD COLUMN b integer',
        "ALTER TABLE bar ADD COLUMN a integer /* don't */, ADD COLUMN b integer",
        'ALTER TABLE bar ADD COLUMN a integer /* a, b */, ADD COLUMN b integer',
        'ALTER TABLE bar ADD COLUMN a integer /* first\n   line, second */, ADD COLUMN b integer',
        'ALTER TABLE bar ADD COLUMN a integer /* NOT NULL */',
        'ALTER TABLE bar ADD COLUMN a integer -- NOT NULL',
        'ALTER TABLE bar ADD COLUMN a integer NOT NULL DEFAULT 0 -- not null, no default',
        "ALTER TABLE bar ADD COLUMN a text NOT NULL DEFAULT 'x -- y', ADD COLUMN b integer",
        "ALTER TABLE bar ADD COLUMN a text NOT NULL DEFAULT '/* x', ADD COLUMN b integer",
        "ALTER TABLE bar ADD COLUMN a text NOT NULL DEFAULT 'it''s -- fine', ADD COLUMN b integer",
        'ALTER TABLE bar ADD COLUMN "a--b" integer, ADD COLUMN c integer',
        'ALTER TABLE bar ADD COLUMN a integer -- trailing, no newline',
        'UPDATE bar SET c = 1 -- trailing; no newline',
        'UPDATE bar SET c = 1; /* between */ INSERT INTO bar (c) VALUES (2) -- end',
        'ALTER TABLE bar/**/ADD COLUMN a integer',
    ],
    ids=[
        'apostrophe-in-a-line-comment-between-add-columns',
        'comma-in-a-line-comment-between-add-columns',
        'apostrophe-in-a-block-comment-between-add-columns',
        'comma-in-a-block-comment-between-add-columns',
        'block-comment-across-lines',
        'not-null-in-a-block-comment',
        'not-null-in-a-line-comment',
        'not-null-words-in-a-comment-after-a-default',
        'line-comment-marker-inside-quotes',
        'block-comment-opener-inside-quotes',
        'comment-marker-after-an-escaped-quote',
        'comment-marker-inside-a-quoted-identifier',
        'line-comment-at-the-end-with-no-newline',
        'semicolon-inside-a-trailing-comment',
        'comments-between-and-after-statements',
        'empty-block-comment-separates-tokens',
    ],
)
def test_a_comment_does_not_change_the_verdict_of_additive_sql(statement: str):
    """The other side: comments are inert, and comment markers inside quotes are text, not comments."""
    assert _messages(_revision(upgrade=f'op.execute({statement!r})')) == []


@pytest.mark.parametrize(
    'statement', [f'op.execute({statement!r})' for statement, _ in COMMENT_REFUSED.values()], ids=COMMENT_REFUSED.keys()
)
def test_each_comment_refusal_passes_when_marked_or_in_downgrade(statement: str):
    assert _messages(_revision(upgrade=statement)), 'precondition: refused unmarked in upgrade'
    assert _messages(_revision(upgrade=statement, prelude='# additive-exception: tj-abc123')) == []
    assert _messages(_revision(downgrade=statement)) == []


# --- dollar quoting is refused, not read (tj-7zjov1.47) ---------------------------------------------

DOLLAR_QUOTE = (
    'raw SQL contains a dollar-quote delimiter, which the lint does not read; '
    f'rewrite it without dollar quoting or mark the revision; {FIX_HINT}'
)

# The measured hole: the -- inside the body was read as a comment, blanked the closing $$ and the
# semicolon, and the ALTER joined the CREATE and passed the allowlist. Postgres runs it as its own statement.
PROBE = 'CREATE FUNCTION f() RETURNS void LANGUAGE sql AS $$ SELECT 1 -- $$;\nALTER TABLE t DROP c'

DOLLAR_REFUSED = {
    'probe-comment-inside-the-body': PROBE,
    'benign-function-alone': 'CREATE FUNCTION f() RETURNS integer LANGUAGE sql AS $$ SELECT 1 $$',
    'commented-function-with-a-tag': (
        '-- the trigger body\n'
        'CREATE FUNCTION f() RETURNS trigger LANGUAGE plpgsql AS $body$ BEGIN RETURN NEW; END $body$'
    ),
    'do-block': "DO $$ BEGIN RAISE NOTICE 'x'; END $$",
    'before-a-destructive-statement': 'CREATE FUNCTION f() RETURNS integer AS $$ SELECT 1 $$; DROP TABLE bar',
    # Fail closed (tj-aw0tuk): the check reads the original text, so a delimiter in a quote or comment counts.
    'inside-an-ordinary-quote': "UPDATE bar SET c = '$$'",
    'inside-a-line-comment': 'UPDATE bar SET c = 1 -- $$',
}


@pytest.mark.parametrize('statement', DOLLAR_REFUSED.values(), ids=DOLLAR_REFUSED.keys())
def test_raw_sql_containing_a_dollar_quote_delimiter_is_refused_as_its_own_finding(statement: str):
    """One finding, the dollar-quote one, ahead of the denylist and the allowlist, at the execute line."""
    call = f'op.execute({statement!r})'
    source = _revision(upgrade=call)
    findings = lint_source(Path('rev.py'), source)
    assert [f.message for f in findings] == [DOLLAR_QUOTE]
    assert findings[0].line == source.splitlines().index(f'    {call}') + 1


def test_exec_driver_sql_with_dollar_quoting_is_refused_too():
    upgrade = f'op.get_bind().exec_driver_sql({PROBE!r})'
    assert _messages(_revision(upgrade=upgrade)) == [DOLLAR_QUOTE]


@pytest.mark.parametrize(
    'statement',
    [
        "UPDATE t SET a = 'x$y' WHERE b = $1",
        'INSERT INTO bar (a, b) VALUES ($1, $2)',
        "UPDATE bar SET c = 'cost: $ 5'",
        "UPDATE bar SET c = '$abc'",
    ],
    ids=['positional-parameter-and-lone-dollar', 'two-positional-parameters', 'bare-dollar', 'unclosed-tag'],
)
def test_a_dollar_sign_that_is_not_a_delimiter_is_not_refused(statement: str):
    """Positional parameters and a $ with no closing $ are not dollar quoting; the statement stays additive."""
    assert _messages(_revision(upgrade=f'op.execute({statement!r})')) == []


@pytest.mark.parametrize('statement', DOLLAR_REFUSED.values(), ids=DOLLAR_REFUSED.keys())
def test_dollar_quoting_passes_when_marked_or_in_downgrade(statement: str):
    call = f'op.execute({statement!r})'
    assert _messages(_revision(upgrade=call)), 'precondition: refused unmarked in upgrade'
    assert _messages(_revision(upgrade=call, prelude='# additive-exception: tj-abc123')) == []
    assert _messages(_revision(downgrade=call)) == []


def test_main_exits_1_on_the_dollar_quote_probe(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    bad = tmp_path / 'probe.py'
    call = f'op.execute({PROBE!r})'
    source = _revision(upgrade=call)
    bad.write_text(source, encoding='utf-8')
    assert main([str(bad)]) == 1
    out = capsys.readouterr()
    assert f'{bad}:{source.splitlines().index(f"    {call}") + 1}: {DOLLAR_QUOTE}' in out.err
    assert '1 revision(s), 1 finding(s)' in out.out


# --- the marker ------------------------------------------------------------------------------------

EVERY_BREACH = (
    "op.drop_table('bar')\n    op.execute(UNDEFINED)\n"
    "    op.add_column('bar', sa.Column('feed', sa.Integer(), nullable=False))"
)


@pytest.mark.parametrize(
    'prelude',
    ['# additive-exception: tj-abc123', '"""Revision.\n\n# additive-exception: tj-abc123\n"""'],
    ids=['comment', 'inside-the-docstring'],
)
def test_a_marked_revision_passes_whatever_it_does(prelude: str):
    source = _revision(upgrade=EVERY_BREACH, prelude=prelude)
    assert marker_of(source) == 'tj-abc123'
    assert lint_source(Path('rev.py'), source) == []


@pytest.mark.parametrize(
    'prelude',
    [
        '    # additive-exception: tj-abc123',
        '# additive-exception:',
        '# additive exception: tj-abc123',
        "NOTE = 'see # additive-exception: tj-abc123'",
    ],
    ids=['indented', 'no-bead-id', 'misspelt', 'not-at-line-start'],
)
def test_a_marker_not_at_the_start_of_a_line_or_without_an_id_is_no_marker(prelude: str):
    source = _revision(upgrade=EVERY_BREACH, prelude=prelude)
    assert marker_of(source) is None
    assert len(_messages(source)) == 3


@pytest.mark.parametrize(
    'sample', ['#additive-exception: tj-x', 'x # additive-exception: tj-x'], ids=['no-space-after-hash', 'after-code']
)
def test_a_marker_needs_hash_space_at_the_line_start(sample: str):
    """Two near-misses the marker regex refuses, kept when its old counterpart (the seed check's copy) went.

    The seed check that keyed on the same marker (tj-vhboky.55) was deleted with the old chain (ADR tj-kg521d), so
    there is no second pattern left to agree with; the samples it compared stay pinned against marker_of alone.
    """
    assert marker_of(sample) is None
    assert marker_of('x\n# additive-exception: tj-x\n') == 'tj-x'  # the well-formed marker, mid-file


# --- files, the real tree and the exit status ------------------------------------------------------


def test_the_alpha_baseline_is_the_one_revision_and_passes_unmarked():
    """ADR tj-kg521d B3: the baseline is create-only, so it passes the gate with no marker, and no marker is in the tree."""
    files = revision_files([VERSIONS_DIR])
    assert [path.name for path in files] == [BASELINE_FILE]
    (baseline,) = files
    source = baseline.read_text(encoding='utf-8')
    assert marker_of(source) is None, 'the alpha baseline carries an additive-exception marker'
    assert lint_source(baseline, source) == []


def test_the_baseline_upgrade_would_be_refused_if_it_ever_dropped_something():
    """Non-vacuity of the pass above: the same file with one drop appended to upgrade() is refused, unmarked."""
    path = VERSIONS_DIR / BASELINE_FILE
    source = path.read_text(encoding='utf-8')
    # upgrade() is the body right above downgrade(): one more statement at its end.
    hostile = source.replace(
        '\n\n\ndef downgrade() -> None:', "\n    op.drop_table('panel')\n\n\ndef downgrade() -> None:", 1
    )
    assert hostile != source, 'the baseline no longer ends upgrade() right above downgrade()'
    messages = [finding.message for finding in lint_source(path, hostile)]
    assert len(messages) == 1 and messages[0].startswith('drop_table'), messages


@pytest.mark.parametrize(
    'upgrade',
    [DESTRUCTIVE_OPS['drop_column'], "op.alter_column('bar', 'volume', type_=sa.BigInteger())"],
    ids=['drop-column', 'retype'],
)
def test_a_marked_revision_file_passes_main_and_fails_it_once_the_marker_is_stripped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], upgrade: str
):
    """Marked revisions are synthetic fixtures since the alpha baseline: none is left in the tree (ADR tj-kg521d).

    The marker machinery stays for later revisions, so it is exercised end to end through main on files on disk.
    """
    marked = tmp_path / 'marked.py'
    marked.write_text(_revision(upgrade=upgrade, prelude='# additive-exception: tj-abc123'), encoding='utf-8')
    assert main([str(marked)]) == 0
    marked.write_text(_strip_marker(marked.read_text(encoding='utf-8')), encoding='utf-8')
    capsys.readouterr()
    assert main([str(marked)]) == 1
    assert '1 revision(s), 1 finding(s)' in capsys.readouterr().out


def test_revision_files_reads_directories_and_files_and_skips_package_init(tmp_path: Path):
    (tmp_path / '__init__.py').write_text('', encoding='utf-8')
    (tmp_path / 'b_rev.py').write_text('', encoding='utf-8')
    (tmp_path / 'a_rev.py').write_text('', encoding='utf-8')
    (tmp_path / 'notes.txt').write_text('', encoding='utf-8')
    single = tmp_path / 'other' / 'c_rev.py'
    single.parent.mkdir()
    single.write_text('', encoding='utf-8')
    assert revision_files([tmp_path, single]) == [tmp_path / 'a_rev.py', tmp_path / 'b_rev.py', single]


def test_main_exits_1_and_names_each_finding_on_stderr(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    bad = tmp_path / 'bad.py'
    source = _revision(upgrade=DESTRUCTIVE_OPS['drop_column'])
    bad.write_text(source, encoding='utf-8')
    (tmp_path / 'good.py').write_text(_revision(), encoding='utf-8')
    assert main([str(tmp_path)]) == 1
    out = capsys.readouterr()
    line = source.splitlines().index(f'    {DESTRUCTIVE_OPS["drop_column"]}') + 1
    assert f'{bad}:{line}: drop_column drops a column' in out.err
    assert '2 revision(s), 1 finding(s)' in out.out


def test_main_exits_0_when_nothing_is_found(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    (tmp_path / 'good.py').write_text(_revision(downgrade=DESTRUCTIVE_OPS['drop_table']), encoding='utf-8')
    assert main([str(tmp_path)]) == 0
    assert capsys.readouterr().err == ''


def test_main_defaults_to_the_store_versions_and_passes_them(capsys: pytest.CaptureFixture[str]):
    assert main([]) == 0
    assert f'{len(revision_files([VERSIONS_DIR]))} revision(s), 0 finding(s)' in capsys.readouterr().out


def test_main_refuses_an_empty_selection_rather_than_passing_it(tmp_path: Path):
    """A gate pointed at nothing must not report green: a moved versions directory would silence it."""
    assert main([str(tmp_path)]) == 1


def test_a_finding_renders_as_path_line_message():
    assert Finding(Path('v/rev.py'), 12, 'drop_table drops a table').render() == 'v/rev.py:12: drop_table drops a table'
