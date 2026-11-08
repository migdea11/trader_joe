"""The committed head seed matches the models it is loaded into: the P49 amend of f2b7d4a91c36 (tj-7zjov1.143).

THE DESIGN: the amend drops ingest_request.listing_id and adds a nullable ingest_run.vendor_asset_id, and the seed
cast (seeds/ingest_records.py) sends every counted attempt with the vendor asset id except the FAILED one with no
stats, so the seed holds both values of the new column. The seed guard (test_seed_guard.py) and the producer tests
check that a seed exists, is paired, names its revision and holds the cast's row counts; none of them reads the
columns, so a seed produced before a column change passed all of them. This module closes that gap.

RELATIONSHIPS, NOT CONSTANTS (ADR tj-8fxxfb): the column set of every ingest table in the seed, in the SQL and in the
manifest's digests, is compared with the model's own columns; the vendor asset id is compared with the cast's own
constant. The checks are plain functions over the seed text, so each one can be run against an older seed to see it
red.

PR-GATE TIER: files only, no database. That the seed loads and upgrades is tests/system's.
"""

import json
import re

import pytest

from common.tests.roots import REPO_ROOT
from data.store.app.database.models.ingest_instance import IngestInstance
from data.store.app.database.models.ingest_request import IngestRequest
from data.store.app.database.models.ingest_run import IngestRun
from data.store.seeds.ingest_records import VENDOR_ASSET
from data.store.seeds.manifest import head_revision
from data.store.seeds.producer import VERSIONS_DIR


pytestmark = pytest.mark.data_store

HEAD = head_revision(VERSIONS_DIR)
SEEDS_DIR = REPO_ROOT / 'tests' / 'system' / 'seeds'
MODELS = {model.TABLE_NAME: model.__table__ for model in (IngestInstance, IngestRequest, IngestRun)}

# dump.py renders each row as INSERT INTO public.<table> (<columns>) VALUES (<quote_nullable values>);
_INSERT = re.compile(r'^INSERT INTO public\.(\w+) \(([^)]*)\) VALUES \((.*)\);$')
# quote_nullable gives NULL, '<text>' with '' for a quote, or E'<text>' when the text holds a backslash.
_VALUE = re.compile(r"NULL|E?'(?:[^']|'')*'")


def _values(text: str) -> list[str | None]:
    values = []
    position = 0
    while position < len(text):
        match = _VALUE.match(text, position)
        assert match, f'unreadable value at {position}: {text[position : position + 40]!r}'
        token = match.group(0)
        values.append(None if token == 'NULL' else token[token.index("'") + 1 : -1].replace("''", "'"))
        position = match.end()
        if text.startswith(', ', position):
            position += 2
    return values


def seed_rows(sql: str) -> dict[str, list[dict[str, str | None]]]:
    """The seed's rows of the ingest tables, as column to value, parsed from the dump's INSERT lines."""
    rows: dict[str, list[dict[str, str | None]]] = {table: [] for table in MODELS}
    for line in sql.splitlines():
        match = _INSERT.match(line)
        if match and match[1] in MODELS:
            columns = match[2].split(', ')
            values = _values(match[3])
            assert len(columns) == len(values), line[:120]
            rows[match[1]].append(dict(zip(columns, values, strict=True)))
    return rows


def column_problems(sql: str, manifest: dict) -> list[str]:
    """Every ingest table's columns, in each INSERT and in the manifest's digests, against the model's."""
    problems = []
    for table, rows in seed_rows(sql).items():
        declared = {column.name for column in MODELS[table].columns}
        if not rows:
            problems.append(f'{table}: no row in the seed')
        for seen in {frozenset(row) for row in rows}:
            if seen != declared:
                problems.append(f'{table} INSERT: extra {sorted(seen - declared)}, missing {sorted(declared - seen)}')
        digested = set(manifest['digests'][table])
        if digested != declared:
            problems.append(
                f'{table} manifest: extra {sorted(digested - declared)}, missing {sorted(declared - digested)}'
            )
    return problems


def vendor_asset_problems(sql: str) -> list[str]:
    """Among the attempt rows, the cast's vendor asset id at least once and NULL at least once, and nothing else."""
    attempts = [row for row in seed_rows(sql)[IngestRun.TABLE_NAME] if row.get('attempt_no') is not None]
    if not attempts:
        return ['ingest_run: no attempt row in the seed']
    if any('vendor_asset_id' not in row for row in attempts):
        return ['ingest_run: the seed has no vendor_asset_id column']
    values = [row['vendor_asset_id'] for row in attempts]
    problems = []
    if VENDOR_ASSET not in values:
        problems.append(f'ingest_run: no attempt row holds {VENDOR_ASSET!r}')
    if None not in values:
        problems.append('ingest_run: no attempt row holds NULL (the cast sends one with no id)')
    if strays := sorted({value for value in values if value not in (VENDOR_ASSET, None)}):
        problems.append(f'ingest_run: vendor_asset_id values the cast never sends: {strays}')
    return problems


def _read(revision: str) -> tuple[str, dict]:
    sql = (SEEDS_DIR / f'{revision}.sql').read_text(encoding='utf-8')
    manifest = json.loads((SEEDS_DIR / f'{revision}.json').read_text(encoding='utf-8'))
    return sql, manifest


def test_the_head_seed_has_exactly_the_models_columns_in_every_ingest_table():
    """No listing_id in ingest_request, vendor_asset_id in ingest_run: in the SQL and in the manifest."""
    sql, manifest = _read(HEAD)
    assert column_problems(sql, manifest) == []
    assert 'listing_id' not in sql
    assert all('listing_id' not in columns for columns in manifest['digests'].values())


def test_the_head_seed_holds_both_values_of_the_attempt_vendor_asset_id():
    sql, _ = _read(HEAD)
    assert vendor_asset_problems(sql) == []


def test_the_value_parser_reads_null_quotes_and_escape_strings():
    """The checks above are only as good as this reading of dump.py's quote_nullable output."""
    assert _values("NULL, 'a, b', 'it''s', E'x\\\\y', ''") == [None, 'a, b', "it's", 'x\\\\y', '']


def test_the_checks_are_red_on_a_seed_from_before_the_amend():
    """A synthetic pre-amend seed: listing_id on the request, no vendor_asset_id on the run."""
    request = {column.name for column in MODELS[IngestRequest.TABLE_NAME].columns} | {'listing_id'}
    run = {column.name for column in MODELS[IngestRun.TABLE_NAME].columns} - {'vendor_asset_id'}

    def insert(table: str, columns: set[str]) -> str:
        ordered = sorted(columns)
        values = ', '.join("'1'" if name == 'attempt_no' else 'NULL' for name in ordered)
        return f'INSERT INTO public.{table} ({", ".join(ordered)}) VALUES ({values});'

    instance = {column.name for column in MODELS[IngestInstance.TABLE_NAME].columns}
    sql = '\n'.join(
        [
            insert(IngestInstance.TABLE_NAME, instance),
            insert(IngestRequest.TABLE_NAME, request),
            insert(IngestRun.TABLE_NAME, run),
        ]
    )
    manifest = {
        'digests': {
            IngestInstance.TABLE_NAME: dict.fromkeys(instance, ''),
            IngestRequest.TABLE_NAME: dict.fromkeys(request, ''),
            IngestRun.TABLE_NAME: dict.fromkeys(run, ''),
        }
    }

    assert column_problems(sql, manifest) == [
        "ingest_request INSERT: extra ['listing_id'], missing []",
        "ingest_request manifest: extra ['listing_id'], missing []",
        "ingest_run INSERT: extra [], missing ['vendor_asset_id']",
        "ingest_run manifest: extra [], missing ['vendor_asset_id']",
    ]
    assert vendor_asset_problems(sql) == ['ingest_run: the seed has no vendor_asset_id column']
