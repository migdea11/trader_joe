"""The schema-catalogue diff the downgrade leg relies on finds what a broken downgrade leaves (bead tj-7zjov1.99).

Design checked against: bead tj-7zjov1.99 (P33c) and the P33 architect gate on tj-7zjov1.25 -- an empty downgrade
passed the generic rule, because it compared only the two seeded tables' rows -- and store CLAUDE.md m7 item 17.

PR-GATE TIER: no database. tests/system/test_alpha_baseline_schema_pg.py compares, on Postgres, the schema the alpha
baseline builds from empty with the one metadata.create_all builds (ADR tj-kg521d B5), through
server/common/tests/schema_catalog.py; the downgrade leg these tests were written for went with the old migration chain,
and the shapes below (a dropped column, a changed predicate, a missing label) are the same drift either comparison
must see. The system suite cannot run in
the PR gate, so the diff's teeth are pinned here on synthetic snapshots built through the same catalogue_from_rows the
live read uses: a dropped column, a changed index predicate, a missing enum label, a changed default, and the shape an
empty downgrade() leaves (the new table, its columns, its indexes and its enum types still there). And the allowlisted
comparison (unexplained_differences): an allowlisted revision must differ by exactly its listed facts, each as stated,
and anything else beside them is still reported.
"""

import copy

import pytest

from common.tests.schema_catalog import (
    CATALOGUE_QUERIES,
    CATEGORIES,
    FACT_NAMES,
    VERSION_TABLE,
    AllowedDifference,
    allowed_difference_problems,
    catalogue_from_rows,
    diff_catalogues,
    unexplained_differences,
)


pytestmark = pytest.mark.data_store

ATTEMPT_PREDICATE = '((request_id IS NOT NULL) AND (attempt_no IS NOT NULL))'


def _rows(**overrides: list[tuple]) -> dict[str, list[tuple]]:
    """A parent-like snapshot's rows: one table with a defaulted column, one partial index, one enum, one sequence."""
    rows: dict[str, list[tuple]] = {
        'tables': [('ingest_run', 'r')],
        'columns': [
            ('ingest_run', 'id', 'integer', True, "nextval('ingest_run_id_seq'::regclass)", '', ''),
            ('ingest_run', 'state', 'character varying', True, "'RUNNING'::character varying", '', ''),
            ('ingest_run', 'request_id', 'uuid', False, None, '', ''),
        ],
        'constraints': [('ingest_run', 'ingest_run_pkey', 'p', 'PRIMARY KEY (id)')],
        'indexes': [
            (
                'ingest_run',
                'ingest_run_pkey',
                'CREATE UNIQUE INDEX ingest_run_pkey ON public.ingest_run USING btree (id)',
                None,
            ),
            (
                'ingest_run',
                'ix_ingest_run_request',
                'CREATE INDEX ix_ingest_run_request ON public.ingest_run USING btree (request_id) '
                'WHERE (request_id IS NOT NULL)',
                '(request_id IS NOT NULL)',
            ),
        ],
        'enums': [('feed', ['iex', 'sip', 'otc'])],
        'sequences': [('ingest_run_id_seq', 'integer', 1, 1, 1, 2147483647, False, 'ingest_run', 'id')],
        'types': [],
        'triggers': [],
        'functions': [],
        'extensions': [('plpgsql', '1.0')],
    }
    rows.update(overrides)
    return rows


def _edit(rows: dict[str, list[tuple]], category: str, index: int, position: int, value) -> dict[str, list[tuple]]:
    edited = copy.deepcopy(rows)
    row = list(edited[category][index])
    row[position] = value
    edited[category][index] = tuple(row)
    return edited


PARENT = _rows()


def test_identical_snapshots_differ_in_nothing():
    assert diff_catalogues(catalogue_from_rows(PARENT), catalogue_from_rows(copy.deepcopy(PARENT))) == []


def test_every_category_the_bead_names_is_read():
    assert set(CATEGORIES) >= {'tables', 'columns', 'constraints', 'indexes', 'enums', 'sequences'}
    assert set(CATEGORIES) == set(CATALOGUE_QUERIES)


def test_every_table_scoped_query_leaves_the_version_table_out():
    """alembic_version is alembic's bookkeeping: present after `downgrade base`, absent on a never-migrated database."""
    for category in ('tables', 'columns', 'constraints', 'indexes', 'triggers'):
        assert ':version' in CATALOGUE_QUERIES[category][1], f'{category} does not exclude {VERSION_TABLE}'


def test_a_dropped_column_is_reported_as_only_in_the_parent():
    downgraded = _rows(columns=PARENT['columns'][:2])
    lines = diff_catalogues(
        catalogue_from_rows(PARENT), catalogue_from_rows(downgraded), expected_label='parent', actual_label='downgraded'
    )
    assert lines == ["columns ('ingest_run', 'request_id'): only in parent ('uuid', False, None, '', '')"]


def test_a_changed_index_predicate_is_reported():
    downgraded = _edit(PARENT, 'indexes', 1, 3, None)
    downgraded = _edit(
        downgraded, 'indexes', 1, 2, 'CREATE INDEX ix_ingest_run_request ON public.ingest_run USING btree (request_id)'
    )
    lines = diff_catalogues(catalogue_from_rows(PARENT), catalogue_from_rows(downgraded))
    assert len(lines) == 1 and lines[0].startswith("indexes ('ingest_run', 'ix_ingest_run_request'):"), lines
    assert '(request_id IS NOT NULL)' in lines[0]


def test_only_the_predicate_changing_is_still_a_difference():
    """The predicate is compared in its own right, not only through the rendered definition."""
    downgraded = _edit(PARENT, 'indexes', 1, 3, ATTEMPT_PREDICATE)
    assert diff_catalogues(catalogue_from_rows(PARENT), catalogue_from_rows(downgraded)) != []


def test_a_missing_enum_label_is_reported():
    downgraded = _rows(enums=[('feed', ['iex', 'sip'])])
    lines = diff_catalogues(catalogue_from_rows(PARENT), catalogue_from_rows(downgraded))
    assert lines == ["enums ('feed',): expected (('iex', 'sip', 'otc'),) but actual (('iex', 'sip'),)"]


def test_enum_label_order_counts():
    downgraded = _rows(enums=[('feed', ['sip', 'iex', 'otc'])])
    assert diff_catalogues(catalogue_from_rows(PARENT), catalogue_from_rows(downgraded)) != []


def test_a_changed_default_is_reported():
    downgraded = _edit(PARENT, 'columns', 1, 4, None)
    lines = diff_catalogues(catalogue_from_rows(PARENT), catalogue_from_rows(downgraded))
    assert len(lines) == 1 and lines[0].startswith("columns ('ingest_run', 'state'):"), lines


@pytest.mark.parametrize(('position', 'value'), [(2, 'text'), (3, False)], ids=['type', 'nullability'])
def test_a_changed_column_type_or_nullability_is_reported(position, value):
    downgraded = _edit(PARENT, 'columns', 1, position, value)
    assert len(diff_catalogues(catalogue_from_rows(PARENT), catalogue_from_rows(downgraded))) == 1


def test_a_changed_sequence_or_constraint_is_reported():
    sequence = _edit(PARENT, 'sequences', 0, 8, None)  # the sequence no longer owned by its column
    constraint = _edit(PARENT, 'constraints', 0, 3, 'PRIMARY KEY (id, state)')
    assert len(diff_catalogues(catalogue_from_rows(PARENT), catalogue_from_rows(sequence))) == 1
    assert len(diff_catalogues(catalogue_from_rows(PARENT), catalogue_from_rows(constraint))) == 1


def test_an_empty_downgrade_leaves_the_new_table_its_indexes_columns_and_enum_types_behind():
    """What a revision with `return` for a downgrade body leaves: everything its upgrade created."""
    downgraded = copy.deepcopy(PARENT)
    downgraded['tables'].append(('ingest_instance', 'r'))
    downgraded['columns'] += [
        ('ingest_instance', 'instance_id', 'character varying', True, None, '', ''),
        ('ingest_run', 'attempt_no', 'integer', False, None, '', ''),
    ]
    downgraded['constraints'].append(('ingest_instance', 'ingest_instance_pkey', 'p', 'PRIMARY KEY (instance_id)'))
    downgraded['indexes'].append(
        (
            'ingest_run',
            'uq_ingest_run_request_id_attempt_no',
            'CREATE UNIQUE INDEX uq_ingest_run_request_id_attempt_no ON public.ingest_run USING btree '
            '(request_id, attempt_no) WHERE ((request_id IS NOT NULL) AND (attempt_no IS NOT NULL))',
            ATTEMPT_PREDICATE,
        )
    )
    downgraded['enums'].append(('ingest_request_state', ['PENDING', 'QUEUED']))

    lines = diff_catalogues(
        catalogue_from_rows(PARENT), catalogue_from_rows(downgraded), expected_label='parent', actual_label='downgraded'
    )

    assert all(': only in downgraded ' in line for line in lines), lines
    named = {line.split(':', 1)[0] for line in lines}
    assert named == {
        "tables ('ingest_instance',)",
        "columns ('ingest_instance', 'instance_id')",
        "columns ('ingest_run', 'attempt_no')",
        "constraints ('ingest_instance', 'ingest_instance_pkey')",
        "indexes ('ingest_run', 'uq_ingest_run_request_id_attempt_no')",
        "enums ('ingest_request_state',)",
    }


# ---------------------------------------------------------------------------------------------
# An allowlisted comparison differs by EXACTLY its listed facts (architect gate on tj-7zjov1.99, ruling (c)): a NOT
# NULL column on one side and nullable on the other, here on ingest_run.state (the shape of the alpha baseline's two
# relaxed reason columns against the old chain, ADR tj-kg521d section 4).

STATE = ('ingest_run', 'state')
STATE_NULLABLE = AllowedDifference('columns', STATE, 'not_null', expected=True, actual=False)
NULLABLE_STATE = _edit(PARENT, 'columns', 1, 3, False)


def _unexplained(downgraded, allowed=(STATE_NULLABLE,)):
    return unexplained_differences(
        catalogue_from_rows(PARENT),
        catalogue_from_rows(downgraded),
        allowed,
        expected_label='parent',
        actual_label='downgraded',
    )


def test_exactly_the_allowed_fact_differing_is_accepted():
    assert diff_catalogues(catalogue_from_rows(PARENT), catalogue_from_rows(NULLABLE_STATE)) != []
    assert _unexplained(NULLABLE_STATE) == []


def test_the_allowed_fact_not_differing_is_reported_as_not_seen():
    """An entry that outlives its reason: the downgrade now restores NOT NULL."""
    lines = _unexplained(copy.deepcopy(PARENT))
    assert len(lines) == 1 and lines[0].startswith("allowed columns ('ingest_run', 'state') not_null True -> False"), (
        lines
    )
    assert 'not seen' in lines[0]


def test_the_allowed_fact_differing_the_other_way_is_reported():
    nullable_parent = _rows(columns=NULLABLE_STATE['columns'])
    lines = unexplained_differences(
        catalogue_from_rows(nullable_parent), catalogue_from_rows(PARENT), (STATE_NULLABLE,)
    )
    assert any('not seen' in line for line in lines), lines


def test_another_fact_of_the_same_column_differing_is_reported():
    """The allowance covers nullability alone, not the column: a changed type beside it fails."""
    retyped = _edit(NULLABLE_STATE, 'columns', 1, 2, 'text')
    lines = _unexplained(retyped)
    assert lines == [
        "columns ('ingest_run', 'state'): parent ('character varying', False, \"'RUNNING'::character varying\", '', '') "
        "but downgraded ('text', False, \"'RUNNING'::character varying\", '', '')"
    ]


@pytest.mark.parametrize(
    'other',
    [
        pytest.param(lambda rows: _edit(rows, 'columns', 2, 3, True), id='another-column-nullability'),
        pytest.param(lambda rows: _rows(columns=rows['columns'], enums=[('feed', ['iex', 'sip'])]), id='enum-label'),
        pytest.param(
            lambda rows: _rows(columns=rows['columns'], tables=[*PARENT['tables'], ('extra', 'r')]), id='table'
        ),
        pytest.param(lambda rows: _edit(rows, 'indexes', 1, 3, None), id='index-predicate'),
    ],
)
def test_any_other_difference_beside_the_allowed_one_is_reported(other):
    lines = _unexplained(other(NULLABLE_STATE))
    assert len(lines) == 1 and 'not seen' not in lines[0], lines


def test_the_allowed_column_missing_on_one_side_is_reported():
    dropped = _rows(columns=[PARENT['columns'][0], PARENT['columns'][2]])
    lines = _unexplained(dropped)
    assert any('not seen' in line for line in lines), lines
    assert any(line.startswith("columns ('ingest_run', 'state'): only in parent") for line in lines), lines


@pytest.mark.parametrize(
    ('allowed', 'problem'),
    [
        pytest.param((), 'no allowed differences', id='empty'),
        pytest.param((AllowedDifference('columns', STATE, 'nullable', True, False),), 'is not a fact', id='fact'),
        pytest.param((AllowedDifference('enums', ('feed',), 'labels', (), ()),), 'is not a fact', id='category'),
        pytest.param((AllowedDifference('columns', STATE, 'not_null', True, True),), 'both sides', id='no-change'),
        pytest.param((STATE_NULLABLE, STATE_NULLABLE), 'listed twice', id='duplicate'),
    ],
)
def test_a_malformed_allowance_is_refused(allowed, problem):
    assert any(problem in line for line in allowed_difference_problems(allowed))
    with pytest.raises(ValueError, match=problem):
        _unexplained(NULLABLE_STATE, allowed)


def test_the_column_fact_names_cover_a_column_row_exactly():
    width = CATALOGUE_QUERIES['columns'][0]
    assert all(len(row) == width + len(FACT_NAMES['columns']) for row in PARENT['columns'])
    assert FACT_NAMES['columns'][1] == 'not_null' and PARENT['columns'][1][width + 1] is True


def test_a_missing_category_or_a_duplicate_identity_is_refused():
    rows = _rows()
    del rows['enums']
    with pytest.raises(AssertionError, match='missing'):
        catalogue_from_rows(rows)
    with pytest.raises(AssertionError, match='appears twice'):
        catalogue_from_rows(_rows(tables=[('ingest_run', 'r'), ('ingest_run', 'r')]))
