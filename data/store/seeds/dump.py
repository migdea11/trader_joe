r"""The dump: what normalises the tables, and how Postgres renders them into the seed's .sql.

THIS DOCSTRING IS THE SEED FORMAT'S DEFINITION (decision tj-vhboky.55 SEED FORMAT and addendum S9).
Hand-written bootstrap seeds (tj-vhboky.62) follow it.

The .sql is a DATA-ONLY rendering of store_dataset_entry and stock_market_activity (never the
8f41c2d7a3b9 archive table). It is rendered BY POSTGRES through the driver, in a session at
TimeZone UTC and extra_float_digits 1 (the digests' settings), and holds exactly:

  1. For each table in SEED_TABLES order (entries, then bars), one line per row, rows ordered by id
     ascending, each

         INSERT INTO public.<table> (<columns>) VALUES (<values>);

     <columns> are the table's columns from information_schema, SORTED BY NAME (code-point order,
     not table order), each passed through quote_ident and joined by ', '. <values> are
     quote_nullable(<column>) for the same columns in the same order, joined by ', ': NULL for a
     NULL, otherwise the value's text as a quoted literal (E'...' when it holds a backslash). Every
     line ends with ';' and a newline. A table with no rows contributes no line.
  2. When there are bars, one final line:

         SELECT pg_catalog.setval('public.stock_market_activity_id_seq', <N>, true);

     N is the bar row count (after normalisation the ids are 1..N).

No comments, no SET lines, no blank lines, no psql meta-commands, and the text ends in one newline.
Column order inside an INSERT is alphabetical because INSERTs name their columns (column order
differs between a fresh chain and a downgraded one, tj-uxl817); the loader and the manifest do not
depend on it. check_no_meta_commands REFUSES (DumpRefused) any rendered line that starts with a
backslash, because the loader refuses one and a seed that loads only through psql is not a seed.

The rendering's real output is proven only against Postgres: tj-vhboky.62 loads every committed seed
and checks the recomputed digests, tj-vhboky.65 runs the producer twice.

WHY THE TABLES ARE NORMALISED FIRST. Two runs on fresh databases must render identical bytes, and
three things differ between them: gen_random_uuid() entry ids, now() created_at and updated_at, and
the surrogate serial bar id, which follows the order the ingest batches arrived in. NORMALISE_SQL
therefore rewrites both tables in one transaction: an entry id becomes the md5 of the entry's
natural key rendered as a uuid, a bar's dataset_id follows it, a bar id becomes its rank under its
natural key, and both timestamps become SEED_TIMESTAMP. The scratch database is the caller's: this
is a destructive rewrite of the two tables and nothing else, which is why the producer's refusal to
touch a non-synthetic row runs first.

The rewrite copies with SELECT * and reinserts with INSERT ... SELECT *, so it does not name the
columns and survives a later revision adding one. It does name id, dataset_id, created_at,
updated_at and the entry's natural key; a revision that drops or renames one needs this file
edited, and the producer fails loudly (the script errors) rather than write a seed.
"""

# Every seed row carries this in created_at and updated_at.
SEED_TIMESTAMP = '2000-01-01 00:00:00+00'

# The tables the seed carries, parents first. Fixed: never the archive table.
ENTRY_TABLE = 'store_dataset_entry'
BAR_TABLE = 'stock_market_activity'
SEED_TABLES = (ENTRY_TABLE, BAR_TABLE)

# The entry's natural key, for the deterministic id. Mirrors StoreDatasetEntry.NATURAL_KEY, IN ITS
# ORDER; "end" is quoted because it is a keyword.
#
# feed IS LOAD-BEARING HERE, not just newly present (tj-3mk3u5.31). This hash IS the seeded entry's
# primary key, so a column missing from it is two different datasets hashing to one id -- and feed
# joining the entry's identity is exactly what makes two entries differing in nothing else
# possible (tj-f2qz44). Left out, the seed would fail on a duplicate primary key, or worse dump
# only one of the pair.
_ENTRY_ID_SQL = (
    "md5(concat_ws('|', asset_symbol, source::text, granularity::text, asset_type::text, data_type::text, owner, "
    'expiry_type::text, update_type::text, feed::text, start::text, "end"::text))::uuid'
)

NORMALISE_SQL = f"""
BEGIN;
SET LOCAL TIME ZONE 'UTC';
CREATE TEMP TABLE seed_entry ON COMMIT DROP AS SELECT * FROM {ENTRY_TABLE};
CREATE TEMP TABLE seed_bar ON COMMIT DROP AS SELECT * FROM {BAR_TABLE};
CREATE TEMP TABLE seed_map ON COMMIT DROP AS SELECT id AS old_id, {_ENTRY_ID_SQL} AS new_id FROM seed_entry;
UPDATE seed_bar b SET dataset_id = m.new_id FROM seed_map m WHERE b.dataset_id = m.old_id;
UPDATE seed_entry e SET id = m.new_id FROM seed_map m WHERE e.id = m.old_id;
UPDATE seed_entry SET created_at = TIMESTAMPTZ '{SEED_TIMESTAMP}', updated_at = TIMESTAMPTZ '{SEED_TIMESTAMP}';
UPDATE seed_bar SET created_at = TIMESTAMPTZ '{SEED_TIMESTAMP}', updated_at = TIMESTAMPTZ '{SEED_TIMESTAMP}';
UPDATE seed_bar b SET id = r.rank FROM (
    SELECT id, row_number() OVER (ORDER BY dataset_id, asset_symbol, source, feed, granularity, timestamp) AS rank
    FROM seed_bar
) r WHERE b.id = r.id;
TRUNCATE {ENTRY_TABLE}, {BAR_TABLE};
INSERT INTO {ENTRY_TABLE} SELECT * FROM seed_entry ORDER BY id;
INSERT INTO {BAR_TABLE} SELECT * FROM seed_bar ORDER BY id;
SELECT setval(pg_get_serial_sequence('{BAR_TABLE}', 'id'), max(id), true) FROM {BAR_TABLE} HAVING count(*) > 0;
COMMIT;
"""  # nosec B608 -- interpolates module constants only, never input

# The BAR table's serial sequence, for the position line.
BAR_SEQUENCE = f'public.{BAR_TABLE}_id_seq'


class DumpRefused(Exception):
    r"""The dump holds something a seed must not: a line starting with a backslash."""


def _literal(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def render_table_sql(table: str, columns: list[str]) -> str:
    """The query Postgres answers with one INSERT line per row of a table.

    Args:
        table (str): A seed table, from SEED_TABLES.
        columns (list[str]): The table's column names, from information_schema.

    Returns:
        str: SQL returning one text column, one row per table row, ordered by id. The column list
            and each value are rendered by Postgres (quote_ident, quote_nullable); the only
            interpolations are the module-constant table and the schema's own column names,
            single-quoted with the quote doubled.
    """
    ordered = sorted(columns)
    names = " || ', ' || ".join(f'quote_ident({_literal(column)})' for column in ordered)
    values = " || ', ' || ".join(f'quote_nullable({_quote(column)})' for column in ordered)
    return (
        f"SELECT 'INSERT INTO public.{table} (' || {names} || ') VALUES (' || {values} || ');' "  # nosec B608 -- module-constant table, quoted schema names
        f'FROM public.{table} ORDER BY id;'
    )


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def check_no_meta_commands(text: str) -> None:
    r"""Refuse a dump with any line starting with a backslash.

    The loader refuses one, so a seed carrying one is not a seed. A row value holding a newline
    followed by a backslash is the only way one can arise.

    Args:
        text (str): The rendered dump.

    Raises:
        DumpRefused: If a line starts with a backslash. The message gives the count, never the line.
    """
    refused = sum(1 for line in text.split('\n') if line.lstrip().startswith('\\'))
    if refused:
        raise DumpRefused(f'{refused} line(s) starting with a backslash in the dump')


def with_sequence_position(text: str, bar_count: int) -> str:
    """Append the bar sequence's position line when there are bars.

    After NORMALISE_SQL the bar ids are 1..N, so the position is N and known.

    Args:
        text (str): The rendered INSERT lines, ending in a newline (or empty).
        bar_count (int): Rows in the bar table.

    Returns:
        str: The text, with a setval line for the bar sequence when there are bars.
    """
    if bar_count == 0:
        return text
    return f"{text}SELECT pg_catalog.setval('{BAR_SEQUENCE}', {bar_count}, true);\n"
