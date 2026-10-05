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

import pytest

from data.store.app.database.models.base_market_activity import BaseMarketActivity
from data.store.app.database.models.stock_market_activity import StockMarketActivity
from data.store.app.database.models.store_dataset_entry import StoreDatasetEntry
from data.store.seeds import dump
from data.store.seeds.dump import (
    BAR_SEQUENCE,
    BAR_TABLE,
    ENTRY_TABLE,
    NORMALISE_SQL,
    SEED_TABLES,
    DumpRefused,
    check_no_meta_commands,
    render_table_sql,
    with_sequence_position,
)


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
    prefix, suffix = 'SELECT ', f' FROM public.{table} ORDER BY id;'
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


def test_every_seed_table_renders_its_own_rows_ordered_by_id():
    for table in SEED_TABLES:
        sql = render_table_sql(table, ['id', 'owner'])
        assert sql.startswith(f"SELECT 'INSERT INTO public.{table} (' || ")
        assert sql.endswith(f' FROM public.{table} ORDER BY id;')
        assert render_line(sql, table, {'id': 1, 'owner': 'seed-owner-a'}) == (
            f"INSERT INTO public.{table} (id, owner) VALUES ('1', 'seed-owner-a');"
        )


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


def test_the_seed_tables_are_the_two_market_tables_parents_first():
    assert (StoreDatasetEntry.__tablename__, StockMarketActivity.__tablename__) == SEED_TABLES
    assert (ENTRY_TABLE, BAR_TABLE) == SEED_TABLES
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
    statements = [s.strip() for s in NORMALISE_SQL.split(';') if s.strip()]
    assert statements[0] == 'BEGIN'
    assert statements[-1] == 'COMMIT'
    assert statements[1] == "SET LOCAL TIME ZONE 'UTC'"
    assert f'TRUNCATE {ENTRY_TABLE}, {BAR_TABLE}' in statements


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
