r"""The seed dump (data/store/seeds/dump.py): the render query, the backslash refusal, the sequence line.

WHY THIS FILE EXISTS (validator, tj-irhy0a.21; decision tj-vhboky.55 addendum S9). pg_dump is gone:
the .sql is rendered BY POSTGRES from a query render_table_sql builds, and dump.py's module docstring
is now the seed format's definition (tj-vhboky.62 hand-writes bootstrap seeds to it). So what is
pinned is the query's shape -- columns sorted by name in code-point order, each quote_ident'ed on the
left and quote_nullable'd on the right in the same order, rows ORDER BY id, the table from SEED_TABLES
-- plus the pure helpers (check_no_meta_commands, with_sequence_position) and the names NORMALISE_SQL
mirrors from the models.

WHAT TIER THIS IS. No Postgres. render_line() below evaluates the query's concatenation with Python
models of quote_ident and quote_nullable, to show the query produces the documented line shape; the
models cover the simple cases used here and are not Postgres. THE RENDERING'S REAL OUTPUT IS PROVEN
ONLY AGAINST POSTGRES: tj-vhboky.62 loads every committed seed and checks the recomputed digests
against its manifest, and tj-vhboky.65 runs the producer twice through the MCP. NOT RUN here: that
NORMALISE_SQL or the render query parses.
"""

import itertools
import re
import uuid

import pytest
from sqlalchemy import UniqueConstraint
from sqlalchemy.sql.functions import now as sql_now

from data.store.app.database.models.asset_list import AssetListEvent, AssetListRow, AssetListSnapshot
from data.store.app.database.models.base_market_activity import BaseMarketActivity
from data.store.app.database.models.corporate_action import CorporateAction
from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import IngestRequest
from data.store.app.database.models.ingest_run import IngestRun
from data.store.app.database.models.panel import (
    Panel,
    PanelMemberTransition,
    PanelPass,
    PanelSettingsVersion,
    PanelSourceSettingsVersion,
)
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.seeds import dump
from data.store.seeds.dump import (
    BAR_SEQUENCE,
    BAR_TABLE,
    CORPORATE_ACTION_TABLE,
    ENTRY_TABLE,
    INSTANCE_TABLE,
    NORMALISE_SQL,
    PANEL_PASS_TABLE,
    PANEL_SETTINGS_TABLE,
    PANEL_SOURCE_SETTINGS_TABLE,
    PANEL_TABLE,
    PANEL_TRANSITION_TABLE,
    REQUEST_TABLE,
    RUN_TABLE,
    SEED_TABLES,
    SNAPSHOT_EVENT_TABLE,
    SNAPSHOT_ROW_TABLE,
    SNAPSHOT_TABLE,
    DumpRefused,
    check_no_meta_commands,
    order_column,
    render_table_sql,
    with_sequence_position,
)


SEED_MODELS = (
    StoreDatasetEntry,
    StockMarketActivity,
    IngestInstance,
    IngestRequest,
    IngestRun,
    CorporateAction,
    AssetListSnapshot,
    AssetListRow,
    AssetListEvent,
    Panel,
    PanelSettingsVersion,
    PanelSourceSettingsVersion,
    PanelPass,
    PanelMemberTransition,
)
PANEL_MODELS = SEED_MODELS[-5:]


pytestmark = pytest.mark.data_store


# ---------------------------------------------------------------------------------------------
# A model of the render query's concatenation
# ---------------------------------------------------------------------------------------------


_TERM = re.compile(
    r"""'(?P<literal>(?:[^']|'')*)'"""
    r"""|quote_ident\('(?P<ident>(?:[^']|'')*)'\)"""
    r"""|quote_nullable\("(?P<column>(?:[^"]|"")*)"\)"""
)
_SEPARATOR = re.compile(r'\s*\|\|\s*')


def _pg_quote_ident(name: str) -> str:
    """Postgres quote_ident for the names used here: bare when lower-case safe, else double-quoted."""
    if re.fullmatch(r'[a-z_][a-z0-9_]*', name) and name not in {'end', 'timestamp'}:
        return name
    return '"' + name.replace('"', '""') + '"'


def _pg_quote_nullable(value: object) -> str:
    """Postgres quote_nullable of a value's text: NULL, or a literal (E'' when it holds a backslash)."""
    if value is None:
        return 'NULL'
    text = str(value)
    if '\\' in text:
        return "E'" + text.replace('\\', '\\\\').replace("'", "''") + "'"
    return "'" + text.replace("'", "''") + "'"


def _split(sql: str, table: str) -> str:
    prefix, suffix = 'SELECT ', f' FROM public.{table} ORDER BY {order_column(table)};'
    assert sql.startswith(prefix) and sql.endswith(suffix), sql
    return sql[len(prefix) : -len(suffix)]


def render_line(sql: str, table: str, row: dict[str, object]) -> str:
    """What Postgres answers for one row, by evaluating the SELECT list term by term."""
    expression, out, at = _split(sql, table), [], 0
    while True:
        term = _TERM.match(expression, at)
        assert term is not None, expression[at:]
        if term['literal'] is not None:
            out.append(term['literal'].replace("''", "'"))
        elif term['ident'] is not None:
            out.append(_pg_quote_ident(term['ident'].replace("''", "'")))
        else:
            out.append(_pg_quote_nullable(row[term['column'].replace('""', '"')]))
        at = term.end()
        if at == len(expression):
            return ''.join(out)
        separator = _SEPARATOR.match(expression, at)
        assert separator is not None and separator.end() > at, expression[at:]
        at = separator.end()


# ---------------------------------------------------------------------------------------------
# render_table_sql
# ---------------------------------------------------------------------------------------------


def test_the_render_query_for_a_small_table_is_exactly_this():
    assert render_table_sql('t', ['b', 'a']) == (
        "SELECT 'INSERT INTO public.t (' || quote_ident('a') || ', ' || quote_ident('b') || ') VALUES (' "
        '|| quote_nullable("a") || \', \' || quote_nullable("b") || \');\' FROM public.t ORDER BY id;'
    )


@pytest.mark.parametrize('columns', list(itertools.permutations(['close', 'Zeta', '_x', 'alpha'])))
def test_columns_are_sorted_by_code_point_whatever_order_they_arrive_in(columns):
    """information_schema's ORDER BY follows the database collation; the seed must not."""
    sql = render_table_sql('t', list(columns))
    names = re.findall(r"quote_ident\('([^']*)'\)", sql)
    values = re.findall(r'quote_nullable\("([^"]*)"\)', sql)
    assert names == ['Zeta', '_x', 'alpha', 'close'] == sorted(columns)
    assert values == names, 'the values follow the column list, in the same order'


def test_the_query_renders_the_documented_line():
    sql = render_table_sql('t', ['b', 'id', 'a'])
    line = render_line(sql, 't', {'id': 7, 'a': 'x', 'b': None})
    assert line == "INSERT INTO public.t (a, b, id) VALUES ('x', NULL, '7');"


def test_every_seed_table_renders_its_own_rows_ordered_by_its_order_column():
    for table in SEED_TABLES:
        sql = render_table_sql(table, ['id', 'owner'])
        assert sql.startswith(f"SELECT 'INSERT INTO public.{table} (' || ")
        assert sql.endswith(f' FROM public.{table} ORDER BY {order_column(table)};')
        assert render_line(sql, table, {'id': 1, 'owner': 'seed-owner-a'}) == (
            f"INSERT INTO public.{table} (id, owner) VALUES ('1', 'seed-owner-a');"
        )


def test_the_instance_marker_renders_ordered_by_instance_id():
    """ingest_instance has no id column: ORDER BY id would fail the render on Postgres."""
    sql = render_table_sql(INSTANCE_TABLE, ['instance_id', 'superseded_at'])
    assert sql.endswith(f' FROM public.{INSTANCE_TABLE} ORDER BY instance_id;')


# ---------------------------------------------------------------------------------------------
# order_column
# ---------------------------------------------------------------------------------------------


def test_order_column_is_instance_id_for_the_instance_marker_and_id_elsewhere():
    assert order_column(INSTANCE_TABLE) == 'instance_id'
    assert [order_column(table) for table in (ENTRY_TABLE, BAR_TABLE, REQUEST_TABLE, RUN_TABLE)] == ['id'] * 4
    assert order_column('some_other_table') == 'id'


@pytest.mark.parametrize('model', SEED_MODELS, ids=lambda model: model.__tablename__)
def test_order_column_is_each_seed_tables_one_primary_key_column(model):
    """A total, database-independent order needs a unique key: the primary key, whatever it is called."""
    (key,) = model.__table__.primary_key.columns
    assert order_column(model.__tablename__) == key.name


def test_a_hostile_column_name_stays_a_quoted_name_on_both_sides():
    """The only non-constant interpolation (nosec B608, dump.py): single-quoted left, double-quoted right."""
    hostile = 'it\'s" || pg_sleep(1) || "'
    sql = render_table_sql('t', [hostile, 'a'])
    assert "quote_ident('it''s\" || pg_sleep(1) || \"')" in sql
    assert 'quote_nullable("it\'s"" || pg_sleep(1) || """)' in sql
    assert render_line(sql, 't', {hostile: 'v', 'a': 'w'}) == (
        'INSERT INTO public.t (a, "it\'s"" || pg_sleep(1) || """) VALUES (\'w\', \'v\');'
    )


def test_values_are_rendered_by_quote_nullable_never_by_python():
    """No row value passes through Python formatting: every value term is a quote_nullable call."""
    sql = render_table_sql('t', ['a', 'b'])
    left, right = sql.split(" || ') VALUES (' || ")
    assert 'quote_nullable' not in left and 'quote_ident' not in right
    assert right.count('quote_nullable(') == 2


# ---------------------------------------------------------------------------------------------
# check_no_meta_commands
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    'line', ['\\connect trader_joe', '\\.', '\\restrict abc', '  \\set ON_ERROR_STOP off', '\t\\!rm -rf /', '\\']
)
def test_any_line_starting_with_a_backslash_is_refused(line):
    with pytest.raises(DumpRefused):
        check_no_meta_commands(f'SELECT 1;\n{line}\nSELECT 2;\n')


def test_the_refusal_counts_and_never_quotes_the_line():
    with pytest.raises(DumpRefused) as raised:
        check_no_meta_commands("\\copy x from 'SECRETVALUE'\n\\echo SECRETVALUE\nSELECT 1;\n")
    assert str(raised.value) == '2 line(s) starting with a backslash in the dump'


def test_a_backslash_inside_a_statement_is_accepted():
    check_no_meta_commands("INSERT INTO t (a) VALUES (E'a\\\\b');\n")


def test_a_value_with_a_newline_then_a_backslash_is_how_a_refused_line_arises():
    """Postgres keeps a newline inside a literal, so the next rendered line can start with one."""
    line = render_line(render_table_sql('t', ['a']), 't', {'a': 'x\n\\y'})
    with pytest.raises(DumpRefused):
        check_no_meta_commands(line + '\n')


def test_an_ordinary_rendering_passes():
    sql = render_table_sql(BAR_TABLE, ['close', 'id'])
    text = ''.join(render_line(sql, BAR_TABLE, {'id': i, 'close': 1.5}) + '\n' for i in (1, 2))
    check_no_meta_commands(with_sequence_position(text, 2))


# ---------------------------------------------------------------------------------------------
# with_sequence_position
# ---------------------------------------------------------------------------------------------


def test_bars_gain_one_setval_line_at_the_bar_count():
    text = 'INSERT INTO public.stock_market_activity (id) VALUES (1);\n'
    assert with_sequence_position(text, 7) == text + f"SELECT pg_catalog.setval('{BAR_SEQUENCE}', 7, true);\n"


def test_the_line_is_appended_whatever_the_text_holds():
    """The render never emits a setval, so the line is always added when there are bars (builder's call)."""
    assert with_sequence_position('', 3) == f"SELECT pg_catalog.setval('{BAR_SEQUENCE}', 3, true);\n"


def test_no_bars_means_no_setval():
    text = 'INSERT INTO public.store_dataset_entry (id) VALUES (1);\n'
    assert with_sequence_position(text, 0) == text


def test_the_sequence_is_the_bar_tables_serial():
    """Postgres names a serial column's sequence <table>_<column>_seq; the bar id is a serial."""
    assert StockMarketActivity.__table__.c.id.autoincrement in (True, 'auto')
    assert f'public.{StockMarketActivity.__tablename__}_id_seq' == BAR_SEQUENCE


# ---------------------------------------------------------------------------------------------
# The format's definition, in dump.py's docstring
# ---------------------------------------------------------------------------------------------


def test_the_docstring_documents_the_lines_this_module_renders():
    """tj-vhboky.62 writes bootstrap seeds from the docstring; its templates must be the real ones."""
    doc = dump.__doc__
    assert 'INSERT INTO public.<table> (<columns>) VALUES (<values>);' in doc
    assert "SELECT pg_catalog.setval('public.stock_market_activity_id_seq', <N>, true);" in doc
    assert (
        with_sequence_position('', 5) == "SELECT pg_catalog.setval('public.stock_market_activity_id_seq', 5, true);\n"
    )
    for rule in ('SORTED BY NAME', 'code-point order', 'quote_ident', 'quote_nullable', 'ordered by id'):
        assert rule in doc, rule
    assert 'tj-vhboky.62' in doc and 'tj-vhboky.65' in doc


def test_the_pg_dump_era_names_are_gone():
    for gone in ('PG_DUMP_ARGS', 'filter_dump'):
        assert not hasattr(dump, gone)


# ---------------------------------------------------------------------------------------------
# What NORMALISE_SQL names, against the models it mirrors
# ---------------------------------------------------------------------------------------------


def test_the_seed_tables_are_the_market_tables_then_the_ingest_tables():
    """Bead tj-7zjov1.100: entries, bars, instances, requests, runs; then corporate actions, then the asset lists.

    Corporate actions since tj-7zjov1.31; the asset-list snapshots, their rows and their events since tj-7zjov1.30; the
    five panel tables, parents first, since tj-7zjov1.196.
    """
    assert tuple(model.__tablename__ for model in SEED_MODELS) == SEED_TABLES
    assert (
        ENTRY_TABLE,
        BAR_TABLE,
        INSTANCE_TABLE,
        REQUEST_TABLE,
        RUN_TABLE,
        CORPORATE_ACTION_TABLE,
        SNAPSHOT_TABLE,
        SNAPSHOT_ROW_TABLE,
        SNAPSHOT_EVENT_TABLE,
        PANEL_TABLE,
        PANEL_SETTINGS_TABLE,
        PANEL_SOURCE_SETTINGS_TABLE,
        PANEL_PASS_TABLE,
        PANEL_TRANSITION_TABLE,
    ) == SEED_TABLES
    assert not any('archive' in table for table in SEED_TABLES)


def test_the_entry_id_hashes_the_models_natural_key_in_its_order():
    """A missing column lets two entries share an id, and the reinsert fails."""
    inner = re.search(r"concat_ws\('\|', (.*?)\)\)::uuid", dump._ENTRY_ID_SQL, re.S)
    assert inner is not None
    columns = [part.strip().split('::')[0].strip('"') for part in inner.group(1).split(',')]
    assert tuple(columns) == StoreDatasetEntry.NATURAL_KEY


def test_the_bar_rank_orders_by_the_models_natural_key():
    """A rank over anything less than the unique key is not a total order, and ties break by heap order."""
    order = re.search(r'row_number\(\) OVER \(ORDER BY ([^)]*)\)', NORMALISE_SQL)
    assert order is not None
    assert tuple(column.strip() for column in order.group(1).split(',')) == BaseMarketActivity.NATURAL_KEY


def test_the_rewrite_is_one_transaction_at_utc_over_both_tables():
    """It carries its own BEGIN and COMMIT, which is why the connection autocommits (stack.py)."""
    # Whitespace-collapsed: the TRUNCATE spans two source lines since the asset-list tables joined it (tj-7zjov1.30).
    statements = [' '.join(s.split()) for s in NORMALISE_SQL.split(';') if s.strip()]
    assert statements[0] == 'BEGIN'
    assert statements[-1] == 'COMMIT'
    assert statements[1] == "SET LOCAL TIME ZONE 'UTC'"
    assert f'TRUNCATE {", ".join(SEED_TABLES)}' in statements
    assert sum(1 for statement in statements if statement.startswith('TRUNCATE')) == 1


def test_both_timestamps_of_both_tables_become_the_constant():
    for temp in ('seed_entry', 'seed_bar'):
        assert re.search(
            rf'UPDATE {temp} SET created_at = TIMESTAMPTZ \'{re.escape(dump.SEED_TIMESTAMP)}\', '
            rf'updated_at = TIMESTAMPTZ \'{re.escape(dump.SEED_TIMESTAMP)}\';',
            NORMALISE_SQL,
        ), temp


def test_rows_are_reinserted_in_id_order_and_the_sequence_follows():
    assert f'INSERT INTO {ENTRY_TABLE} SELECT * FROM seed_entry ORDER BY id;' in NORMALISE_SQL
    assert f'INSERT INTO {BAR_TABLE} SELECT * FROM seed_bar ORDER BY id;' in NORMALISE_SQL
    assert f"setval(pg_get_serial_sequence('{BAR_TABLE}', 'id'), max(id), true)" in NORMALISE_SQL


@pytest.mark.parametrize(
    ('table', 'temp'),
    [
        (INSTANCE_TABLE, 'seed_instance'),
        (REQUEST_TABLE, 'seed_request'),
        (RUN_TABLE, 'seed_run'),
        (SNAPSHOT_TABLE, 'seed_snapshot'),
        (SNAPSHOT_ROW_TABLE, 'seed_snapshot_row'),
        (SNAPSHOT_EVENT_TABLE, 'seed_snapshot_event'),
        (PANEL_TABLE, 'seed_panel'),
        (PANEL_SETTINGS_TABLE, 'seed_panel_settings'),
        (PANEL_SOURCE_SETTINGS_TABLE, 'seed_panel_source_settings'),
        (PANEL_PASS_TABLE, 'seed_panel_pass'),
        (PANEL_TRANSITION_TABLE, 'seed_panel_transition'),
    ],
)
def test_each_ingest_table_is_copied_and_reinserted_in_its_order(table, temp):
    assert f'CREATE TEMP TABLE {temp} ON COMMIT DROP AS SELECT * FROM {table};' in NORMALISE_SQL
    assert f'INSERT INTO {table} SELECT * FROM {temp} ORDER BY {order_column(table)};' in NORMALISE_SQL


def _is_now(column) -> bool:
    default = column.server_default
    return default is not None and isinstance(getattr(default, 'arg', None), sql_now)


@pytest.mark.parametrize(
    ('model', 'temp'),
    [
        (IngestInstance, 'seed_instance'),
        (IngestRequest, 'seed_request'),
        (IngestRun, 'seed_run'),
        (AssetListSnapshot, 'seed_snapshot'),
        (AssetListEvent, 'seed_snapshot_event'),
        (Panel, 'seed_panel'),
        (PanelSettingsVersion, 'seed_panel_settings'),
        (PanelSourceSettingsVersion, 'seed_panel_source_settings'),
        (PanelPass, 'seed_panel_pass'),
        (PanelMemberTransition, 'seed_panel_transition'),
    ],
)
def test_every_column_postgres_stamps_with_now_is_rewritten(model, temp):
    """A now() default left as stamped differs between two runs, and the seed is no longer deterministic."""
    stamped = [column.name for column in model.__table__.columns if _is_now(column)]
    assert stamped, 'the model has a now() default this test reads'
    for name in stamped:
        # Inside one UPDATE of the temp copy (no ';' between), the column is assigned.
        assert re.search(rf'UPDATE {temp}( \w+)? SET [^;]*?\b{name} = ', NORMALISE_SQL), name


def test_the_finished_and_superseded_instants_are_rewritten_too():
    """Stamped with now() by the code, not by a default: the request's finish, the instance's supersession."""
    assert re.search(r"UPDATE seed_request SET finished_at = TIMESTAMPTZ '[^']+' \+ interval '1 hour'", NORMALISE_SQL)
    assert re.search(r'superseded_at = CASE WHEN superseded_at IS NULL THEN NULL ELSE ', NORMALISE_SQL)
    assert re.search(r"UPDATE seed_run SET started_at = TIMESTAMPTZ '[^']+', finished_at = ", NORMALISE_SQL)


def test_the_random_run_ids_become_the_md5_of_a_rank_over_the_rows_content():
    """gen_random_uuid() ids differ every run; the rank orders by the request and attempt first, then the labels."""
    assert f"UPDATE seed_run r SET id = md5('{RUN_TABLE}|' || k.rank::text)::uuid" in NORMALISE_SQL
    order = re.search(
        r'UPDATE seed_run r SET id .*?row_number\(\) OVER \(\s*ORDER BY (.*?)\) AS rank', NORMALISE_SQL, re.S
    )
    assert order is not None
    columns = [part.strip() for part in order.group(1).split(',')]
    assert columns[:2] == ['request_id NULLS FIRST', 'attempt_no NULLS FIRST']
    assert {name.split()[0] for name in columns} <= {column.name for column in IngestRun.__table__.columns}


@pytest.mark.parametrize('temp', ['seed_request', 'seed_run'])
def test_an_ingest_rows_dataset_id_follows_the_entry_map(temp):
    assert f'UPDATE {temp} r SET dataset_id = m.new_id FROM seed_map m WHERE r.dataset_id = m.old_id;' in NORMALISE_SQL


# ---------------------------------------------------------------------------------------------
# The asset-list snapshots (tj-7zjov1.30): random ids replaced by keys of their own content, references following
# ---------------------------------------------------------------------------------------------


def test_the_asset_list_row_table_has_no_now_default_so_none_is_rewritten():
    """The row table stamps nothing; if a now() default is added there, the parametrised test above must grow."""
    assert not [column.name for column in AssetListRow.__table__.columns if _is_now(column)]


def test_a_snapshot_id_becomes_the_md5_of_the_delivering_attempts_unique_key():
    """(request_id, attempt_no) is the snapshot's unique index, so the new id is unique and the same every run."""
    (unique,) = [index for index in AssetListSnapshot.__table__.indexes if index.unique]
    assert tuple(column.name for column in unique.columns) == ('request_id', 'attempt_no')
    assert (
        f"SELECT id AS old_id, md5('{SNAPSHOT_TABLE}|' || request_id::text || '|' || attempt_no::text)::uuid AS new_id "
        'FROM seed_snapshot;'
    ) in ' '.join(NORMALISE_SQL.split())
    assert 'UPDATE seed_snapshot s SET id = m.new_id' in NORMALISE_SQL


@pytest.mark.parametrize(
    ('temp', 'column'),
    [
        ('seed_snapshot_row', 'snapshot_id'),
        ('seed_snapshot_event', 'snapshot_id'),
        ('seed_snapshot_event', 'previous_snapshot_id'),
    ],
)
def test_every_reference_to_a_snapshot_follows_the_snapshot_map(temp, column):
    """A row or event left pointing at the random id would point at no snapshot after the rewrite."""
    alias = temp.removeprefix('seed_snapshot_')[0]
    assert (
        f'UPDATE {temp} {alias} SET {column} = m.new_id FROM seed_snapshot_map m WHERE {alias}.{column} = m.old_id;'
        in NORMALISE_SQL
    )


def test_every_uuid_column_of_the_asset_list_tables_is_a_snapshot_reference_or_a_rewritten_id():
    """Derived from the models: a new uuid column that the rewrite does not map stays random and breaks determinism."""
    handled = {
        SNAPSHOT_TABLE: {'id', 'request_id'},
        SNAPSHOT_ROW_TABLE: {'id', 'snapshot_id'},
        SNAPSHOT_EVENT_TABLE: {'id', 'snapshot_id', 'previous_snapshot_id'},
    }
    for model in (AssetListSnapshot, AssetListRow, AssetListEvent):
        uuids = {column.name for column in model.__table__.columns if column.type.python_type is uuid.UUID}
        assert uuids == handled[model.__tablename__], model.__tablename__


def test_the_row_and_event_ids_are_the_md5_of_their_content_after_the_snapshot_ids_moved():
    """The row id hashes (snapshot, vendor id, symbol); the event id its unique key (snapshot, vendor id, kind)."""
    (event_key,) = [index for index in AssetListEvent.__table__.indexes if index.unique]
    assert tuple(column.name for column in event_key.columns) == ('snapshot_id', 'vendor_asset_id', 'kind')
    row_id = (
        f"UPDATE seed_snapshot_row SET id = md5('{SNAPSHOT_ROW_TABLE}|' || snapshot_id::text || '|' || vendor_asset_id "
        "|| '|' || symbol)::uuid;"
    )
    event_id = (
        f"UPDATE seed_snapshot_event SET id = md5('{SNAPSHOT_EVENT_TABLE}|' || snapshot_id::text || '|' || "
        "vendor_asset_id || '|' || kind::text)::uuid,"
    )
    assert row_id in NORMALISE_SQL
    assert event_id in NORMALISE_SQL
    # After the map, so the hash is over the stable snapshot id, never the random one.
    assert NORMALISE_SQL.index(row_id) > NORMALISE_SQL.index('UPDATE seed_snapshot_row r SET snapshot_id')
    assert NORMALISE_SQL.index(event_id) > NORMALISE_SQL.index('UPDATE seed_snapshot_event e SET snapshot_id')


# ---------------------------------------------------------------------------------------------
# The panel cast (tj-7zjov1.196): ids from unique keys or the pass rank, references following, parents first
# ---------------------------------------------------------------------------------------------
#
# NOT RUN here: that the panel block parses and runs on Postgres. No SQL parser is in the environment, so what is
# pinned is each statement's text against the models; the parse and the byte-identical second run are the stack's
# (the orchestrator's seed_dump runs, tj-7zjov1.197 phase 2).

PANEL_TEMPS = {
    PANEL_TABLE: 'seed_panel',
    PANEL_SETTINGS_TABLE: 'seed_panel_settings',
    PANEL_SOURCE_SETTINGS_TABLE: 'seed_panel_source_settings',
    PANEL_PASS_TABLE: 'seed_panel_pass',
    PANEL_TRANSITION_TABLE: 'seed_panel_transition',
}
# The alias each panel temp table carries in its UPDATEs.
PANEL_ALIASES = {
    'seed_panel': 'p',
    'seed_panel_settings': 's',
    'seed_panel_source_settings': 's',
    'seed_panel_pass': 'p',
    'seed_panel_transition': 't',
}
# The map each panel table's random id is moved through.
PANEL_MAPS = {
    PANEL_TABLE: 'seed_panel_map',
    PANEL_SETTINGS_TABLE: 'seed_settings_map',
    PANEL_SOURCE_SETTINGS_TABLE: 'seed_source_settings_map',
    PANEL_PASS_TABLE: 'seed_pass_map',
}
# Every uuid column of the panel tables but the table's own id: the map it follows, or None for a literal of the cast
# (a request id: ingest_records.py writes request ids as literals).
PANEL_REFERENCES = {
    (PANEL_SETTINGS_TABLE, 'panel_id'): 'seed_panel_map',
    (PANEL_PASS_TABLE, 'panel_id'): 'seed_panel_map',
    (PANEL_PASS_TABLE, 'settings_version_id'): 'seed_settings_map',
    (PANEL_PASS_TABLE, 'source_settings_version_id'): 'seed_source_settings_map',
    (PANEL_PASS_TABLE, 'active_snapshot_id'): 'seed_snapshot_map',
    (PANEL_PASS_TABLE, 'inactive_snapshot_id'): 'seed_snapshot_map',
    (PANEL_TRANSITION_TABLE, 'pass_id'): 'seed_pass_map',
    (PANEL_TRANSITION_TABLE, 'panel_id'): 'seed_panel_map',
    (PANEL_TRANSITION_TABLE, 'dataset_id'): 'seed_map',
    (PANEL_TRANSITION_TABLE, 'request_id'): None,
}


def _statements() -> list[str]:
    """NORMALISE_SQL's statements, whitespace-collapsed."""
    return [' '.join(statement.split()) for statement in NORMALISE_SQL.split(';') if statement.strip()]


def _position(prefix: str) -> int:
    """The index of the one statement starting with prefix."""
    (found,) = [index for index, statement in enumerate(_statements()) if statement.startswith(prefix)]
    return found


def _statement(prefix: str) -> str:
    return _statements()[_position(prefix)]


def _reference_update(table: str, column: str) -> str:
    temp = PANEL_TEMPS[table]
    alias = PANEL_ALIASES[temp]
    target = PANEL_REFERENCES[(table, column)]
    return f'UPDATE {temp} {alias} SET {column} = m.new_id FROM {target} m WHERE {alias}.{column} = m.old_id'


def _unique_key(model) -> tuple[str, ...]:
    """The columns of the model's one unique constraint or unique index."""
    keys = [
        tuple(column.name for column in constraint.columns)
        for constraint in model.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    ] + [tuple(column.name for column in index.columns) for index in model.__table__.indexes if index.unique]
    (key,) = keys
    return key


def test_every_uuid_column_of_the_panel_tables_is_a_mapped_reference_a_literal_or_a_rewritten_id():
    """Derived from the models: a new uuid column the rewrite does not handle stays random and breaks determinism."""
    for model in PANEL_MODELS:
        uuids = {column.name for column in model.__table__.columns if column.type.python_type is uuid.UUID}
        handled = {'id'} | {column for table, column in PANEL_REFERENCES if table == model.__tablename__}
        assert uuids == handled, model.__tablename__


@pytest.mark.parametrize(
    ('table', 'column'), [key for key, target in PANEL_REFERENCES.items() if target], ids=lambda value: str(value)
)
def test_every_panel_reference_follows_its_map(table, column):
    """A reference left at the random id points at no row after the rewrite (and a foreign key refuses the insert)."""
    assert _reference_update(table, column) in _statements()


def test_every_foreign_key_of_the_panel_tables_follows_the_map_of_the_table_it_names():
    """Derived from the models' foreign keys, so a new one is caught: the reference's map is its target table's map."""
    keys = [
        (model.__tablename__, key.parent.name, key.column.table.name)
        for model in PANEL_MODELS
        for key in model.__table__.foreign_keys
    ]
    assert len(keys) == 6, 'the panel models carry the foreign keys this test reads'
    for table, column, target in keys:
        assert PANEL_REFERENCES[(table, column)] == PANEL_MAPS[target], (table, column)


def test_every_map_is_built_before_any_panel_row_is_rewritten():
    """A map built after an UPDATE moved its key reads the moved value, and every reference misses."""
    first_update = min(i for i, statement in enumerate(_statements()) if statement.startswith('UPDATE seed_panel'))
    for name in [*PANEL_MAPS.values(), 'seed_snapshot_map', 'seed_map']:
        assert _position(f'CREATE TEMP TABLE {name} ON COMMIT DROP AS') < first_update, name


def test_every_panel_id_moves_after_every_reference_to_it_followed():
    """An id moved first leaves the map's old_id matching nothing: the references follow, then the id moves."""
    for table, name in PANEL_MAPS.items():
        temp = PANEL_TEMPS[table]
        moves = _position(f'UPDATE {temp} {PANEL_ALIASES[temp]} SET id = m.new_id')
        for (source, column), target in PANEL_REFERENCES.items():
            if target == name:
                assert _position(_reference_update(source, column)) < moves, (source, column)


def test_a_panel_id_is_the_md5_of_its_unique_name():
    assert _unique_key(Panel) == ('name',)
    assert _statement('CREATE TEMP TABLE seed_panel_map') == (
        f"CREATE TEMP TABLE seed_panel_map ON COMMIT DROP AS SELECT id AS old_id, md5('{PANEL_TABLE}|' || name)::uuid "
        'AS new_id FROM seed_panel'
    )


def test_a_settings_version_id_is_the_md5_of_its_unique_key_over_the_new_panel_id():
    """(panel_id, version_no) is unique; the hash takes the panel's NEW id, so it is the same in every run."""
    assert _unique_key(PanelSettingsVersion) == ('panel_id', 'version_no')
    assert _statement('CREATE TEMP TABLE seed_settings_map') == (
        'CREATE TEMP TABLE seed_settings_map ON COMMIT DROP AS SELECT s.id AS old_id, '
        f"md5('{PANEL_SETTINGS_TABLE}|' || m.new_id::text || '|' || s.version_no::text)::uuid AS new_id "
        'FROM seed_panel_settings s JOIN seed_panel_map m ON m.old_id = s.panel_id'
    )


def test_a_source_settings_version_id_is_the_md5_of_its_unique_key():
    assert _unique_key(PanelSourceSettingsVersion) == ('source', 'version_no')
    assert _statement('CREATE TEMP TABLE seed_source_settings_map') == (
        'CREATE TEMP TABLE seed_source_settings_map ON COMMIT DROP AS SELECT id AS old_id, '
        f"md5('{PANEL_SOURCE_SETTINGS_TABLE}|' || source::text || '|' || version_no::text)::uuid AS new_id "
        'FROM seed_panel_source_settings'
    )


def test_the_pass_rank_is_a_total_order_over_not_null_columns_started_at_first():
    """A pass has no natural key, so its rank must be a total order.

    A rank whose columns can be NULL or tie leaves the order to the heap: the id breaks the last tie (the cast's passes
    differ in started_at, one transaction each), and NULL placement never arises.
    """
    order = re.search(
        r'CREATE TEMP TABLE seed_pass_map .*?row_number\(\) OVER \(\s*ORDER BY (.*?)\s*\) AS rank', NORMALISE_SQL, re.S
    )
    assert order is not None
    columns = [part.strip() for part in order.group(1).split(',')]
    assert columns[0] == 'started_at' and columns[-1] == 'id'
    model_columns = {column.name: column for column in PanelPass.__table__.columns}
    for name in columns:
        assert not model_columns[name].nullable, f'{name} is nullable: its NULLs order by the default rule'
    assert f"md5('{PANEL_PASS_TABLE}|' || rank::text)::uuid AS new_id, rank FROM" in _statement(
        'CREATE TEMP TABLE seed_pass_map'
    )


def test_a_transition_id_is_the_md5_of_its_unique_key_after_the_pass_id_moved():
    assert _unique_key(PanelMemberTransition) == ('pass_id', 'seq')
    statement = (
        f"UPDATE seed_panel_transition SET id = md5('{PANEL_TRANSITION_TABLE}|' || pass_id::text || '|' || "
        'seq::text)::uuid'
    )
    assert _position(statement) > _position(_reference_update(PANEL_TRANSITION_TABLE, 'pass_id'))


def test_the_instants_of_a_pass_and_its_transitions_follow_the_rank_and_the_seq():
    """started_at, finished_at (kept NULL for the unfinished pass) and recorded_at, from the rank and seq."""
    stamp = dump.SEED_TIMESTAMP
    assert _statement('UPDATE seed_panel_pass p SET id = m.new_id') == (
        f"UPDATE seed_panel_pass p SET id = m.new_id, started_at = TIMESTAMPTZ '{stamp}' + m.rank * interval '1 hour', "
        f"finished_at = CASE WHEN p.finished_at IS NULL THEN NULL ELSE TIMESTAMPTZ '{stamp}' + m.rank * interval "
        "'1 hour' + interval '10 minutes' END FROM seed_pass_map m WHERE p.id = m.old_id"
    )
    assert _statement('UPDATE seed_panel_transition t SET recorded_at') == (
        f"UPDATE seed_panel_transition t SET recorded_at = TIMESTAMPTZ '{stamp}' + p.rank * interval '1 hour' + "
        "t.seq * interval '1 second' FROM seed_pass_map p WHERE p.new_id = t.pass_id"
    )
    # Joined on the NEW pass id, so it runs after the transitions' pass_id moved.
    assert _position('UPDATE seed_panel_transition t SET recorded_at') > _position(
        _reference_update(PANEL_TRANSITION_TABLE, 'pass_id')
    )


@pytest.mark.parametrize(
    ('temp', 'column'),
    [('seed_panel_settings', 'saved_at'), ('seed_panel_source_settings', 'saved_at'), ('seed_panel', 'created_at')],
)
def test_a_version_instant_and_the_panel_instant_are_literals(temp, column):
    alias = PANEL_ALIASES[temp]
    stamp = dump.SEED_TIMESTAMP
    statement = _statement(f'UPDATE {temp} {alias} SET id = m.new_id')
    expected = (
        f"{column} = TIMESTAMPTZ '{stamp}'"
        if column == 'created_at'
        else f"{column} = TIMESTAMPTZ '{stamp}' + {alias}.version_no * interval '1 hour'"
    )
    assert expected in statement


def test_the_panel_rows_are_reinserted_parents_first():
    """Derived from the foreign keys: a referenced table's INSERT precedes the referencing one's.

    In NORMALISE_SQL and in SEED_TABLES (the dump and manifest order, which a loader replays).
    """
    for model in PANEL_MODELS:
        for key in model.__table__.foreign_keys:
            parent, child = key.column.table.name, model.__tablename__
            assert _position(f'INSERT INTO {parent} SELECT') < _position(f'INSERT INTO {child} SELECT'), child
            assert SEED_TABLES.index(parent) < SEED_TABLES.index(child), child


def test_the_panel_block_has_no_subquery_inside_an_update():
    """A correlated subquery in a SET re-reads per row; the block joins the maps built beforehand instead."""
    updates = [statement for statement in _statements() if statement.startswith('UPDATE seed_panel')]
    assert len(updates) == 15
    for statement in updates:
        assert '(SELECT' not in statement, statement[:80]
