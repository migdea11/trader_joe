"""The alpha baseline on Postgres: `alembic upgrade head` on an EMPTY database builds exactly what the models declare.

Design checked against: ADR tj-kg521d section 5 and gate items B5 and B6. The baseline (one revision, enum labels as
literals in the file) and the models (the truth the service runs on) are two hand-kept descriptions of one schema; this
is the lasting check that they agree, and it closes alembic check's blind spots (CHECKs, enum labels and their ORDER,
server defaults, index predicates), which the old m7 checklist item 14 compared by hand.

HOW. Two scratch databases on the stack's server (the tj-vhboky.62 exception, tests/system/scratch_db.py: created here,
dropped WITH (FORCE) at the end, the stack's own database never touched):
  * UPGRADED: `alembic upgrade head` by the repository's alembic CLI and ini, from empty;
  * CREATED: metadata.create_all of every model module migrations/env.py loads (its ALLOWED_MODELS).
Each is read with server/common/tests/schema_catalog.py from pg_catalog with Postgres's own renderers (columns with
type, NOT NULL, default, identity; constraints with their definition: primary keys, uniques, CHECKs, foreign keys with
ON DELETE; indexes with columns, method, uniqueness and predicate; enum labels IN SORT ORDER; sequences; types;
triggers; functions; extensions) and the two must be IDENTICAL: an empty diff_catalogues, nothing allowed. Column
ordinal position is not compared (schema_catalog's documented exclusion).

Also here, on the UPGRADED database: alembic_version holds exactly the baseline; a second `alembic upgrade head`
succeeds, leaves the revision and changes nothing in the catalogue (the CI from-empty step's no-op, here per database);
and `alembic check` reports no drift (B6), which is the autogenerate comparison the service's metadata makes.

WRITTEN, NOT YET RUN where it was written (tj-7zjov1.362.5, 2026-11-06): it runs with the suite on a stack built from
the baseline (the test-stack window of tj-kg521d). Run by make test-system and the agent-stack MCP's run_system_tests.
FAIL, NEVER SKIP.
"""

import ast
import importlib
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Engine

from common.database.sql_alchemy_table import AppBase
from common.tests.schema_catalog import diff_catalogues, read_catalogue
from tests.system.scratch_db import MIGRATIONS_DIR, ScratchDb, scratch_database, scratch_name


pytestmark = pytest.mark.data_store

BASELINE_REVISION = '5d9a3c7e1b84'


def _env_model_modules() -> list[str]:
    """migrations/env.py's ALLOWED_MODELS, read from its source: the model set alembic itself compares with."""
    tree = ast.parse((MIGRATIONS_DIR / 'env.py').read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, 'id', None) == 'ALLOWED_MODELS' for t in node.targets):
            return sorted(ast.literal_eval(node.value))
    raise AssertionError('migrations/env.py no longer defines ALLOWED_MODELS')


def _model_metadata() -> sa.MetaData:
    for name in _env_model_modules():
        importlib.import_module(f'data.store.app.database.models.{name}')
    return AppBase.DATA_STORE_BASE.metadata


@pytest.fixture
def upgraded(request: pytest.FixtureRequest, pg_settings, pg_engine: Engine, run_identity) -> Iterator[ScratchDb]:
    name = scratch_name('baseline_up', run_identity.run_id, request.node.nodeid)
    with scratch_database(pg_settings, pg_engine, name) as db:
        yield db


@pytest.fixture
def created(request: pytest.FixtureRequest, pg_settings, pg_engine: Engine, run_identity) -> Iterator[ScratchDb]:
    name = scratch_name('baseline_ca', run_identity.run_id, request.node.nodeid, extra='create_all')
    with scratch_database(pg_settings, pg_engine, name) as db:
        yield db


def _catalogue(db: ScratchDb):
    with db.engine.connect() as conn:
        return read_catalogue(conn)


def _upgrade_head(db: ScratchDb) -> None:
    """`alembic upgrade head` as an operator types it, and it lands on the baseline."""
    run = db.alembic('upgrade', 'head')
    assert run.returncode == 0, f'alembic upgrade head on {db.name} failed:\n{run.output[-6000:]}'
    assert db.revision() == BASELINE_REVISION, f'upgrade head left {db.revision()}'


def test_the_checkout_has_one_head_and_it_is_the_baseline():
    assert list(ScriptDirectory(str(MIGRATIONS_DIR)).get_heads()) == [BASELINE_REVISION]


def test_upgrade_head_from_empty_builds_exactly_what_create_all_builds(upgraded: ScratchDb, created: ScratchDb):
    """B5: the two catalogues are identical, category by category; any line of the diff is the drift, named."""
    assert upgraded.revision() is None and created.revision() is None, 'a scratch database was not empty'
    _upgrade_head(upgraded)
    with created.engine.begin() as conn:
        _model_metadata().create_all(conn)

    built, declared = _catalogue(upgraded), _catalogue(created)

    assert built['tables'], 'non-vacuity: the upgrade built no table'
    assert set(built['tables']) == set(declared['tables'])
    lines = diff_catalogues(declared, built, expected_label='create_all', actual_label='upgrade')
    assert lines == [], 'the baseline and the models disagree:\n' + '\n'.join(lines)


def test_the_enum_labels_and_their_order_are_the_models(upgraded: ScratchDb):
    """The one fact alembic check cannot see, stated on its own so a failure reads as what it is."""
    _upgrade_head(upgraded)
    built = _catalogue(upgraded)['enums']
    declared: dict[tuple[str], tuple[tuple[str, ...]]] = {}
    for table in _model_metadata().tables.values():
        for column in table.columns:
            if isinstance(column.type, sa.Enum):
                declared[(column.type.name,)] = (tuple(column.type.enums),)
    assert declared, 'non-vacuity: no enum column in the models'
    assert built == declared


SOURCE_SETTINGS_TABLE = 'panel_source_settings_version'
# ADR tj-2ayia2 (edited IN PLACE under the same id): Postgres's own rendering (pg_get_constraintdef) of the three CHECKs
# that replace ck_panel_source_settings_version_numbers_non_negative. Postgres parenthesises each comparison.
RATE_CHECK_DEFINITIONS = {
    'ck_panel_source_settings_version_ramp_positive': 'CHECK ((ramp_datasets_per_hour >= 1))',
    'ck_panel_source_settings_version_request_cap_min': 'CHECK ((request_cap_per_hour >= 2))',
    'ck_panel_source_settings_version_window_non_negative': (
        'CHECK (((corporate_action_window_pre_days >= 0) AND (corporate_action_window_post_days >= 0)))'
    ),
}


def test_the_edited_baseline_builds_nullable_rates_and_the_three_checks_on_postgres(upgraded: ScratchDb):
    """B5 for the tj-2ayia2 edit, named so a failure reads as what it is.

    The two rate columns are NULLABLE (NULL is no limit), the window columns are not, the three CHECKs are there in
    Postgres's normalised text, and the old numbers_non_negative is gone.
    """
    _upgrade_head(upgraded)
    built = _catalogue(upgraded)

    not_null = {
        column: facts[1] for (table, column), facts in built['columns'].items() if table == SOURCE_SETTINGS_TABLE
    }
    assert not_null['ramp_datasets_per_hour'] is False and not_null['request_cap_per_hour'] is False
    assert not_null['corporate_action_window_pre_days'] is True
    assert not_null['corporate_action_window_post_days'] is True
    assert built['columns'][(SOURCE_SETTINGS_TABLE, 'ramp_datasets_per_hour')][0] == 'integer'
    checks = {
        name: facts[1]
        for (table, name), facts in built['constraints'].items()
        if table == SOURCE_SETTINGS_TABLE and facts[0] == 'c'
    }
    assert {name: checks.get(name) for name in RATE_CHECK_DEFINITIONS} == RATE_CHECK_DEFINITIONS, checks
    assert 'ck_panel_source_settings_version_numbers_non_negative' not in checks, checks


def test_the_rate_checks_behave_on_the_upgraded_schema(upgraded: ScratchDb):
    """A NULL rate passes (no limit), the minimums pass, ramp 0 and a cap of 1 are refused naming their CHECK."""
    _upgrade_head(upgraded)
    insert = sa.text(
        'INSERT INTO panel_source_settings_version (source, version_no, ramp_datasets_per_hour, request_cap_per_hour, '
        "corporate_action_window_pre_days, corporate_action_window_post_days, actor) VALUES ('MANUAL_ENTRY', :n, "
        ":ramp, :cap, 0, 0, 'operator')"
    )
    with upgraded.engine.begin() as conn:
        conn.execute(insert, {'n': 1, 'ramp': None, 'cap': None})
        conn.execute(insert, {'n': 2, 'ramp': 1, 'cap': 2})
    for n, ramp, cap, constraint in (
        (3, 0, None, 'ck_panel_source_settings_version_ramp_positive'),
        (4, None, 1, 'ck_panel_source_settings_version_request_cap_min'),
        (5, None, 0, 'ck_panel_source_settings_version_request_cap_min'),
    ):
        with upgraded.engine.begin() as conn, pytest.raises(sa.exc.IntegrityError) as excinfo:
            conn.execute(insert, {'n': n, 'ramp': ramp, 'cap': cap})
        assert f'violates check constraint "{constraint}"' in str(excinfo.value.orig), excinfo.value.orig


def test_a_second_upgrade_is_a_no_op_and_alembic_check_finds_no_drift(upgraded: ScratchDb):
    """The CI from-empty step's repeat, on one database; then B6, alembic check against the migrated schema."""
    _upgrade_head(upgraded)
    first = _catalogue(upgraded)

    _upgrade_head(upgraded)
    assert _catalogue(upgraded) == first, 'a second upgrade head changed the schema'

    check = upgraded.alembic('check')
    assert check.returncode == 0, f'alembic check found drift on a database fresh from the baseline:\n{check.output}'
